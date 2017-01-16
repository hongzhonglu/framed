from collections import OrderedDict

from framed import solver_instance
from framed.solvers.solver import Status
from framed import FBA, pFBA, MOMA, lMOMA
from framed import load_cbmodel, save_cbmodel


class EnsembleModel():

    def __init__(self, model, size, reaction_states=None):
        self.model = model.copy()
        self.size = size
        self.reaction_states = {}

        if reaction_states:
            for r_id, states in reaction_states.items():
                assert r_id in model.reactions
                assert len(states) == size
                self.reaction_states[r_id] = states[:]
        else:
            self.reaction_states = {r_id: [True]*size for r_id in model.reactions}

    def get_reaction_states(self, i):
        return {r_id: self.reaction_states[r_id][i] if r_id in self.reaction_states else True
                for r_id in self.model.reactions}

    def get_constraints(self, i):
        return {r_id: (0, 0) for r_id in self.model.reactions if r_id in self.reaction_states and
                not self.reaction_states[r_id][i]}


def simulate_ensemble(ensemble, method='FBA', constraints=None):
    solver = solver_instance(ensemble.model)
    flux_sample = OrderedDict([(r_id, [None]*ensemble.size) for r_id in ensemble.model.reactions])

    func = eval(method)

    for i in range(ensemble.size):
        current = ensemble.get_constraints(i)
        current.update(constraints)
        sol = func(ensemble.model, constraints=current, solver=solver)

        if sol.status == Status.OPTIMAL:
            for r_id in ensemble.model.reactions:
                flux_sample[r_id][i] = sol.values[r_id]

    return flux_sample


def save_ensemble(ensemble, outputfile, **kwargs):

    for r_id, states in ensemble.reaction_states.items():
        state_as_str = ' '.join(map(str, map(int, states)))
        ensemble.model.reactions[r_id].metadata['ENSEMBLE_STATE'] = state_as_str

    save_cbmodel(ensemble.model, outputfile, **kwargs)


def load_ensemble(inputfile, **kwargs):

    model = load_cbmodel(inputfile, **kwargs)
    reaction_states = {}

    for r_id, rxn in model.reactions.items():
        if 'ENSEMBLE_STATE' in rxn.metadata:
            state_as_str = rxn.metadata['ENSEMBLE_STATE']
            states = map(bool, map(int, (state_as_str.split())))
            reaction_states[r_id] = states

    sizes = map(len, reaction_states.values())

    if len(set(sizes)) > 1:
        print 'Error: reactions have different ensemble size'
        return

    return EnsembleModel(model, sizes[0], reaction_states)