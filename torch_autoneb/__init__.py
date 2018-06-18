import pickle

import torch
from networkx import MultiGraph, Graph
from torch import optim

from torch_autoneb.hyperparameters import NEBHyperparameters, OptimHyperparameters, AutoNEBHyperparameters, LandscapeExplorationHyperparameters
from torch_autoneb.models import ModelWrapper
from torch_autoneb.neb import NEB
from torch_autoneb.suggest import suggest_pair

try:
    from tqdm import tqdm as pbar
except ModuleNotFoundError:
    class pbar:
        def __init__(self, iterable=None, desc=None, total=None, *args, **kwargs):
            self.iterable = iterable

        def __iter__(self):
            yield from self.iterable

        def __enter__(self):
            pass

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def update(self, N=None):
            pass

__all__ = ["find_minimum", "neb", "auto_neb", "landscape_exploration", "load_pickle_graph"]


def find_minimum(model: ModelWrapper, config: OptimHyperparameters) -> dict:
    optimiser = getattr(optim, config.optim_name)(model.parameters(), **config.optim_args)  # type: optim.Optimizer

    # Initialise
    model.initialise_randomly()
    model.adapt_to_config(config.eval_config)

    # Optimise
    for _ in pbar(range(config.nsteps), "Find mimimum"):
        optimiser.zero_grad()
        model.apply(gradient=True)
        optimiser.step()
    result = {
        "coords": model.get_coords(),
    }

    # Analyse
    result.update(model.analyse())
    return result


def neb(previous_cycle_data, model: ModelWrapper, config: NEBHyperparameters) -> dict:
    # Initialise chain
    previous_path_coords = previous_cycle_data["path_coords"]
    previous_target_distances = previous_cycle_data["target_distances"]
    start_path, target_distances = config.fill_method.fill(previous_path_coords, config.insert_count, previous_target_distances, previous_cycle_data)

    # Model and optimiser
    neb_model = NEB(model, start_path, target_distances)
    optim_config = config.optim_config
    optimiser = getattr(optim, optim_config.optim_name)(neb_model.parameters(), **optim_config.optim_args)  # type: optim.Optimizer

    # Optimise
    for _ in pbar(range(optim_config.nsteps), "NEB"):
        # optimiser.zero_grad()  # has no effect, is overwritten anyway
        neb_model.apply(gradient=True)
        optimiser.step()
    result = {
        "path_coords": neb_model.path_coords.detach().clone(),
        "target_distances": target_distances
    }

    # Analyse
    result.update(neb_model.analyse())
    return result


def auto_neb(m1, m2, graph: MultiGraph, model: ModelWrapper, config: AutoNEBHyperparameters):
    # Continue existing cycles or start from scratch
    existing_edges = graph[m1][m2]
    if len(existing_edges) > 0:
        previous_cycle_idx = max(existing_edges[m1][m2])
        previous_cycle_data = existing_edges[m1][m2][previous_cycle_idx]
        start_cycle_idx = previous_cycle_idx + 1
    else:
        previous_cycle_data = {
            "path_coords": torch.cat([graph.nodes[m]["coords"].view(1, -1) for m in (m1, m2)]),
            "target_distances": torch.ones(1)
        }
        start_cycle_idx = 1
    assert start_cycle_idx <= config.cycle_count

    # Run NEB and add to graph
    for cycle_idx in pbar(range(start_cycle_idx, config.cycle_count + 1)):
        cycle_config = config.hyperparameter_sets[start_cycle_idx - 1]
        connection_data = neb(m1, m2, previous_cycle_data, model, cycle_config)
        graph.add_edge(m1, m2, key=cycle_idx, **connection_data)


def landscape_exploration(graph: MultiGraph, model: ModelWrapper, config: LandscapeExplorationHyperparameters):
    with pbar(desc="Landscape Exploration") as bar:
        while True:
            # Suggest new pair based on current graph
            m1, m2 = suggest_pair(graph, config.value_key, config.weight_key, *config.suggest_engines)
            if m1 is None or m2 is None:
                break
            auto_neb(m1, m2, graph, model, config.auto_neb_config)
            bar.update()


def to_simple_graph(graph: MultiGraph, weight_key: str) -> Graph:
    """
    Reduce the MultiGraph to a simple graph by reducing each multi-edge
    to its lowest container.
    """
    simple_graph = Graph()
    for node in graph:
        simple_graph.add_node(node, **graph.nodes[node])

    for m1 in graph:
        for m2 in graph[m1]:
            best_edge_key = min(graph[m1][m2], key=lambda key: graph[m1][m2][key][weight_key])
            best_edge_data = graph[m1][m2][best_edge_key]
            best_edge_data["cycle_idx"] = best_edge_key
            simple_graph.add_edge(m1, m2, **best_edge_data)

    return simple_graph


def load_pickle_graph(graph_file_name) -> MultiGraph:
    with open(graph_file_name, "rb") as file:
        graph = pickle.load(file)

        # Check file structure
        if not isinstance(graph, MultiGraph):
            raise ValueError(f"{graph_file_name} does not contain a nx.MultiGraph")
    return graph
