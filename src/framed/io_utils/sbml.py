""" This module implements methods for reading and writing SBML files.

@author: Daniel Machado
   
"""

from ..core.model import Model, Metabolite, Reaction, Compartment
from ..core.odemodel import ODEModel
from ..core.cbmodel import CBModel, Gene, Protein, GPRAssociation
from ..core.fixes import fix_cobra_model

from collections import OrderedDict
from sympy.parsing.sympy_parser import parse_expr
from sympy import to_dnf, Or, And
from sympy.logic.boolalg import is_dnf
from libsbml import SBMLReader, SBMLWriter, SBMLDocument, XMLNode, AssignmentRule, parseL3FormulaWithModel, FbcExtension

DEFAULT_SBML_LEVEL = 3
DEFAULT_SBML_VERSION = 1

CB_MODEL = 'cb'
ODE_MODEL = 'ode'

COBRA_MODEL = 'cobra'
FBC2_MODEL = 'fbc2'

LB_TAG = 'LOWER_BOUND'
UB_TAG = 'UPPER_BOUND'
OBJ_TAG = 'OBJECTIVE_COEFFICIENT'
GPR_TAG = 'GENE_ASSOCIATION'

DEFAULT_LOWER_BOUND_ID = 'cobra_default_lb'
DEFAULT_UPPER_BOUND_ID = 'cobra_default_ub'
DEFAULT_ZERO_BOUND_ID = 'cobra_0_bound'

DEFAULT_LOWER_BOUND = -1000
DEFAULT_UPPER_BOUND = 1000

ACTIVATOR_TAG = 'SBO:0000459'
INHIBITOR_TAG = 'SBO:0000020'


def load_sbml_model(filename, kind=None, flavor=None):
    """ Loads a metabolic model from a file.
    
    Arguments:
        filename : String -- SBML file path
        kind : {None (default), CB_MODEL, ODE_MODEL} -- define kind of model to load (optional)
    
    Returns:
        Model -- Simple model or respective subclass
    """
    reader = SBMLReader()
    document = reader.readSBML(filename)
    sbml_model = document.getModel()

    if sbml_model is None:
        raise IOError('Failed to load model.')

    if kind and kind.lower() == CB_MODEL:
        model = _load_cbmodel(sbml_model, flavor)
    elif kind and kind.lower() == ODE_MODEL:
        model = _load_odemodel(sbml_model)
    else:
        model = _load_stoichiometric_model(sbml_model)

    _load_metadata(sbml_model, model)

    return model


def load_cbmodel(filename, flavor=COBRA_MODEL, apply_fixes=True):
    model = load_sbml_model(filename, kind=CB_MODEL, flavor=flavor)

    if apply_fixes:
        fix_cobra_model(model)

    return model


def load_odemodel(filename):
    return load_sbml_model(filename, ODE_MODEL)


def _load_stoichiometric_model(sbml_model):
    model = Model(sbml_model.getId())
    _load_compartments(sbml_model, model)
    _load_metabolites(sbml_model, model)
    _load_reactions(sbml_model, model)
    return model


def _load_compartments(sbml_model, model):
    for compartment in sbml_model.getListOfCompartments():
        model.add_compartment(_load_compartment(compartment))


def _load_compartment(compartment):
    comp = Compartment(compartment.getId(), compartment.getName(), compartment.getSize())
    _load_metadata(compartment, comp)
    return comp


def _load_metabolites(sbml_model, model):
    for species in sbml_model.getListOfSpecies():
        model.add_metabolite(_load_metabolite(species))


def _load_metabolite(species):
    metabolite = Metabolite(species.getId(), species.getName(), species.getCompartment())
    _load_metadata(species, metabolite)
    return metabolite


def _load_reactions(sbml_model, model):
    for reaction in sbml_model.getListOfReactions():
        model.add_reaction(_load_reaction(reaction))


def _load_reaction(reaction):

    stoichiometry = OrderedDict()
    modifiers = OrderedDict()

    for reactant in reaction.getListOfReactants():
        m_id = reactant.getSpecies()
        coeff = -reactant.getStoichiometry()
        if m_id not in stoichiometry:
            stoichiometry[m_id] = coeff
        else:
            stoichiometry[m_id] += coeff

    for product in reaction.getListOfProducts():
        m_id = product.getSpecies()
        coeff = product.getStoichiometry()
        if m_id not in stoichiometry:
            stoichiometry[m_id] = coeff
        else:
            stoichiometry[m_id] += coeff
        if stoichiometry[m_id] == 0.0:
            del stoichiometry[m_id]

    for modifier in reaction.getListOfModifiers():
        m_id = modifier.getSpecies()
        kind = '?'
        sboterm = modifier.getSBOTermID()
        if sboterm == ACTIVATOR_TAG:
            kind = '+'
        if sboterm == INHIBITOR_TAG:
            kind = '-'
        modifiers[m_id] = kind

    rxn = Reaction(reaction.getId(), reaction.getName(), reaction.getReversible(), stoichiometry, modifiers)
    _load_metadata(reaction, rxn)
    return rxn


def _load_cbmodel(sbml_model, flavor):
    model = CBModel(sbml_model.getId())
    _load_compartments(sbml_model, model)
    _load_metabolites(sbml_model, model)
    _load_reactions(sbml_model, model)
    if flavor == COBRA_MODEL:
        _load_cobra_bounds(sbml_model, model)
        _load_cobra_objective(sbml_model, model)
        _load_cobra_gpr(sbml_model, model)
    elif flavor == FBC2_MODEL:
        _load_fbc2_bounds(sbml_model, model)
        _load_fbc2_objective(sbml_model, model)
        _load_fbc2_gpr(sbml_model, model)
    else:
        print 'unsupported SBML flavor', flavor

    return model


def _load_cobra_bounds(sbml_model, model):
    for reaction in sbml_model.getListOfReactions():
        lb = _get_cb_parameter(reaction, LB_TAG)
        ub = _get_cb_parameter(reaction, UB_TAG)
        model.set_flux_bounds(reaction.getId(), lb, ub)


def _load_cobra_objective(sbml_model, model):
    objective = OrderedDict()
    for reaction in sbml_model.getListOfReactions():
        coeff = _get_cb_parameter(reaction, OBJ_TAG, default_value=0)
        if coeff:
            objective[reaction.getId()] = coeff
    model.set_objective(objective)


def _get_cb_parameter(reaction, tag, default_value=None):
    param_value = default_value
    kinetic_law = reaction.getKineticLaw()
    if kinetic_law:
        parameter = kinetic_law.getParameter(tag)
        if parameter:
            param_value = parameter.getValue()
    return param_value


def _load_cobra_gpr(sbml_model, model):
    genes = set()
    gprs = OrderedDict()

    for reaction in sbml_model.getListOfReactions():
        rule = model.reactions[reaction.getId()].metadata.pop(GPR_TAG, None)
        if rule:
            gpr = parse_gpr_rule(rule)
            for protein in gpr.proteins:
                genes |= set(protein.genes)
            gprs[reaction.getId()] = gpr
        else:
            gprs[reaction.getId()] = None

    for gene in sorted(genes):
        model.add_gene(Gene(gene, gene[2:]))

    for r_id, gpr in gprs.items():
        model.set_gpr_association(r_id, gpr)


def parse_gpr_rule(rule):

    if not rule:
        return None

    rule = rule.replace('(', '( ').replace(')', ' )')

    def replacement(token):
        if token == 'and':
            return '&'
        elif token == 'or':
            return '|'
        elif token == '(' or token == ')':
            return token
        elif token.startswith('G_'):
            return token.replace('-', '_')
        else:
            return 'G_' + token.replace('-', '_')

    rule = ' '.join(map(replacement, rule.split()))

    expr = parse_expr(rule)

    if not is_dnf(expr):
        expr = to_dnf(expr)

    gpr = GPRAssociation()

    if type(expr) is Or:
        for sub_expr in expr.args:
            protein = Protein()
            if type(sub_expr) is And:
                protein.genes = [str(gene) for gene in sub_expr.args]
            else:
                protein.genes = [str(sub_expr)]
            gpr.proteins.append(protein)
    elif type(expr) is And:
        protein = Protein()
        protein.genes = [str(gene) for gene in expr.args]
        gpr.proteins = [protein]
    else:
        protein = Protein()
        protein.genes = [str(expr)]
        gpr.proteins = [protein]


    return gpr


def _load_fbc2_bounds(sbml_model, model):
    params = {param.getId(): param.getValue() for param in sbml_model.getListOfParameters()}

    for reaction in sbml_model.getListOfReactions():
        fbc_rxn = reaction.getPlugin('fbc')
        lb = fbc_rxn.getLowerFluxBound()
        ub = fbc_rxn.getUpperFluxBound()
        model.set_flux_bounds(reaction.getId(), params[lb], params[ub])


def _load_fbc2_objective(sbml_model, model):
    fbcmodel = sbml_model.getPlugin('fbc')
    active_obj = fbcmodel.getActiveObjective()
    objective = OrderedDict()
    for rxn_obj in active_obj.getListOfFluxObjectives():
        r_id = rxn_obj.getReaction()
        coeff = rxn_obj.getCoefficient()
        if coeff:
            objective[r_id] = coeff
    model.set_objective(objective)


def _load_fbc2_gpr(sbml_model, model):
    fbcmodel = sbml_model.getPlugin('fbc')

    for gene in fbcmodel.getListOfGeneProducts():
        model.add_gene(Gene(gene.getId(), gene.getName()))

    for reaction in sbml_model.getListOfReactions():
        fbcrxn = reaction.getPlugin('fbc')
        gpr_assoc = fbcrxn.getGeneProductAssociation()
        if gpr_assoc:
            gpr = _parse_fbc_association(gpr_assoc.getAssociation())
            model.set_gpr_association(reaction.getId(), gpr)
        else:
            model.set_gpr_association(reaction.getId(), None)


def _parse_fbc_association(gpr_assoc):

    gpr = GPRAssociation()

    if gpr_assoc.isFbcOr():
        for item in gpr_assoc.getListOfAssociations():
            protein = Protein()
            if item.isFbcAnd():
                for subitem in item.getListOfAssociations():
                    if subitem.isGeneProductRef():
                        protein.genes.append(subitem.getGeneProduct())
                    else:
                        print 'Gene association is not DNF'
            elif item.isGeneProductRef:
                protein.genes.append(item.getGeneProduct())
            else:
                print 'Gene association is not DNF'
            gpr.proteins.append(protein)

    elif gpr_assoc.isFbcAnd():
        protein = Protein()
        for item in gpr_assoc.getListOfAssociations():
            if item.isGeneProductRef():
                protein.genes.append(item.getGeneProduct())
            else:
                print 'Gene association is not DNF'
        gpr.proteins = [protein]
    elif gpr_assoc.isGeneProductRef():
        protein = Protein()
        protein.genes = [gpr_assoc.getGeneProduct()]
        gpr.proteins = [protein]
    else:
        print 'Gene association is not DNF'

    return gpr


def _load_odemodel(sbml_model):
    model = ODEModel(sbml_model.getId())
    _load_compartments(sbml_model, model)
    _load_metabolites(sbml_model, model)
    _load_reactions(sbml_model, model)
    _load_concentrations(sbml_model, model)
    _load_global_parameters(sbml_model, model)
    _load_local_parameters(sbml_model, model)
    _load_ratelaws(sbml_model, model)
    _load_assignment_rules(sbml_model, model)
    model.build_rate_functions()

    return model


def _load_concentrations(sbml_model, model):
    for species in sbml_model.getListOfSpecies():
        model.set_concentration(species.getId(), species.getInitialConcentration())


def _load_global_parameters(sbml_model, model):
    for parameter in sbml_model.getListOfParameters():
            model.set_global_parameter(parameter.getId(), parameter.getValue(), parameter.getConstant())


def _load_local_parameters(sbml_model, model):
    for reaction in sbml_model.getListOfReactions():
        for parameter in reaction.getKineticLaw().getListOfParameters():
            model.set_local_parameter(reaction.getId(), parameter.getId(), parameter.getValue())


def _load_ratelaws(sbml_model, model):
    for reaction in sbml_model.getListOfReactions():
        model.set_ratelaw(reaction.getId(), reaction.getKineticLaw().getFormula())


def _load_assignment_rules(sbml_model, model):
    for rule in sbml_model.getListOfRules():
        if isinstance(rule, AssignmentRule):
            model.set_assignment_rule(rule.getVariable(), rule.getFormula())


def save_sbml_model(model, filename, flavor=None):
    """ Save a model to an SBML file.
    
    Arguments:
        model : Model (or any subclass) -- model
        filename : String -- SBML file path
    """

    document = SBMLDocument(DEFAULT_SBML_LEVEL, DEFAULT_SBML_VERSION)
    if flavor == FBC2_MODEL:
        document.enablePackage(FbcExtension.getXmlnsL3V1V2(), 'fbc', True)
    sbml_model = document.createModel(model.id)
    _save_compartments(model, sbml_model)
    _save_metabolites(model, sbml_model)
    _save_reactions(model, sbml_model)
    if isinstance(model, CBModel):
        _save_cb_parameters(model, sbml_model, flavor)
        _save_gpr_associations(model, sbml_model, flavor)
    if isinstance(model, ODEModel):
        _save_concentrations(model, sbml_model)
        _save_global_parameters(model, sbml_model)
        _save_kineticlaws(model, sbml_model)
        _save_assignment_rules(model, sbml_model)
    _save_metadata(model, sbml_model)
    writer = SBMLWriter()
    writer.writeSBML(document, filename)


def save_cbmodel(model, filename, flavor=COBRA_MODEL):
     save_sbml_model(model, filename, flavor)


def _save_compartments(model, sbml_model):
    for compartment in model.compartments.values():
        sbml_compartment = sbml_model.createCompartment()
        sbml_compartment.setId(compartment.id)
        sbml_compartment.setName(compartment.name)
        sbml_compartment.setSize(compartment.size)
        _save_metadata(compartment, sbml_compartment)


def _save_metabolites(model, sbml_model):
    for metabolite in model.metabolites.values():
        species = sbml_model.createSpecies()
        species.setId(metabolite.id)
        species.setName(metabolite.name)
        species.setCompartment(metabolite.compartment)
        _save_metadata(metabolite, species)


def _save_reactions(model, sbml_model):
    for reaction in model.reactions.values():
        sbml_reaction = sbml_model.createReaction()
        sbml_reaction.setId(reaction.id)
        sbml_reaction.setName(reaction.name)
        sbml_reaction.setReversible(reaction.reversible)
        _save_metadata(reaction, sbml_reaction)

        for m_id, coeff in reaction.stoichiometry.items():
            if coeff < 0:
                speciesReference = sbml_reaction.createReactant()
                speciesReference.setSpecies(m_id)
                speciesReference.setStoichiometry(-coeff)
            elif coeff > 0:
                speciesReference = sbml_reaction.createProduct()
                speciesReference.setSpecies(m_id)
                speciesReference.setStoichiometry(coeff)
        for m_id, kind in reaction.regulators.items():
            speciesReference = sbml_reaction.createModifier()
            speciesReference.setSpecies(m_id)
            if kind == '+':
                speciesReference.setSBOTerm(ACTIVATOR_TAG)
            if kind == '-':
                speciesReference.setSBOTerm(INHIBITOR_TAG)


def _save_cb_parameters(model, sbml_model, flavor):

    if flavor == COBRA_MODEL:
        _save_cobra_parameters(model, sbml_model, set_default_bounds=True)
    elif flavor == FBC2_MODEL:
        _save_fbc_fluxbounds(model, sbml_model)
        _save_fbc_objective(model, sbml_model)
    else:
        _save_cobra_parameters(model, sbml_model)


def _save_gpr_associations(model, sbml_model, flavor):
    if flavor == FBC2_MODEL:
        _save_fbc_gprs(model, sbml_model)
    else:
        _save_cobra_gprs(model, sbml_model)


def _save_cobra_parameters(model, sbml_model, set_default_bounds=False):
    for r_id in model.reactions:
        lb, ub = model.bounds[r_id]
        coeff = model.objective[r_id]
        sbml_reaction = sbml_model.getReaction(r_id)
        kineticLaw = sbml_reaction.createKineticLaw()
        kineticLaw.setFormula('0')
        if set_default_bounds:
            lb = DEFAULT_LOWER_BOUND if lb is None else lb
            ub = DEFAULT_UPPER_BOUND if ub is None else ub
        if lb is not None:
            lbParameter = kineticLaw.createParameter()
            lbParameter.setId(LB_TAG)
            lbParameter.setValue(lb)
        if ub is not None:
            ubParameter = kineticLaw.createParameter()
            ubParameter.setId(UB_TAG)
            ubParameter.setValue(ub)
        objParameter = kineticLaw.createParameter()
        objParameter.setId(OBJ_TAG)
        objParameter.setValue(coeff)


def _save_cobra_gprs(model, sbml_model):
    for r_id, gpr in model.gpr_associations.items():
        if gpr:
            reaction = model.reactions[r_id]
            reaction.metadata[GPR_TAG] = str(gpr)
            sbml_reaction = sbml_model.getReaction(r_id)
            _save_metadata(reaction, sbml_reaction)


def _save_fbc_fluxbounds(model, sbml_model):

    default_lb = sbml_model.createParameter()
    default_lb.setId(DEFAULT_LOWER_BOUND_ID)
    default_lb.setValue(DEFAULT_LOWER_BOUND)

    default_ub = sbml_model.createParameter()
    default_ub.setId(DEFAULT_UPPER_BOUND_ID)
    default_ub.setValue(DEFAULT_UPPER_BOUND)

    zero_bound = sbml_model.createParameter()
    zero_bound.setId(DEFAULT_ZERO_BOUND_ID)
    zero_bound.setValue(0)

    for r_id, (lb, ub) in model.bounds.items():
        fbcrxn = sbml_model.getReaction(r_id).getPlugin('fbc')

        if lb is None or lb <= DEFAULT_LOWER_BOUND:
            fbcrxn.setLowerFluxBound(DEFAULT_LOWER_BOUND_ID)
        elif lb == 0:
            fbcrxn.setLowerFluxBound(DEFAULT_ZERO_BOUND_ID)
        else:
            lb_id = '{}_lower_bound'.format(r_id)
            lb_param = sbml_model.createParameter()
            lb_param.setId(lb_id)
            lb_param.setValue(lb)
            fbcrxn.setLowerFluxBound(lb_id)

        if ub is None or ub >= DEFAULT_UPPER_BOUND:
            fbcrxn.setUpperFluxBound(DEFAULT_UPPER_BOUND_ID)
        elif lb == 0:
            fbcrxn.setUpperFluxBound(DEFAULT_ZERO_BOUND_ID)
        else:
            ub_id = '{}_upper_bound'.format(r_id)
            ub_param = sbml_model.createParameter()
            ub_param.setId(ub_id)
            ub_param.setValue(ub)
            fbcrxn.setUpperFluxBound(ub_id)


def _save_fbc_objective(model, sbml_model):
    fbcmodel = sbml_model.getPlugin('fbc')
    obj = fbcmodel.createObjective()
    for r_id, coeff in model.objective.items():
        if coeff:
            r_obj = obj.createFluxObjective()
            r_obj.setReaction(r_id)
            r_obj.setCoefficient(coeff)


def _save_fbc_gprs(model, sbml_model):
    fbcmodel = sbml_model.getPlugin('fbc')
    for gene in model.genes.values():
        gene_prod = fbcmodel.createGeneProduct()
        gene_prod.setId(gene.id)
        gene_prod.setName(gene.name)

    for r_id, gpr in model.gpr_associations.items():
        if gpr:
            fbcrxn = sbml_model.getReaction(r_id).getPlugin('fbc')
            gpr_assoc = fbcrxn.createGeneProductAssociation()

            if len(gpr.proteins) > 1:
                gpr_assoc = gpr_assoc.createOr()

            for protein in gpr.proteins:
                if len(protein.genes) > 1:
                    protein_assoc = gpr_assoc.createAnd()
                else:
                    protein_assoc = gpr_assoc

                for gene in protein.genes:
                    gene_ref = protein_assoc.createGeneProductRef()
                    gene_ref.setGeneProduct(gene)


def _save_concentrations(model, sbml_model):
    for m_id, value in model.concentrations.items():
        species = sbml_model.getSpecies(m_id)
        species.setInitialConcentration(value)


def _save_global_parameters(model, sbml_model):
    for p_id, value in model.constant_params.items():
        parameter = sbml_model.createParameter()
        parameter.setId(p_id)
        parameter.setValue(value)
        parameter.setConstant(True)
    for p_id, value in model.variable_params.items():
        parameter = sbml_model.createParameter()
        parameter.setId(p_id)
        parameter.setValue(value)
        parameter.setConstant(False)


def _save_kineticlaws(model, sbml_model):
    for r_id, ratelaw in model.ratelaws.items():
        sbml_reaction = sbml_model.getReaction(r_id)
        kineticLaw = sbml_reaction.createKineticLaw()
        #kineticLaw.setFormula(ratelaw)
        kineticLaw.setMath(parseL3FormulaWithModel(ratelaw, sbml_model)) #avoids conversion of Pi to pi
        for p_id, value in model.local_params[r_id].items():
            parameter = kineticLaw.createParameter()
            parameter.setId(p_id)
            parameter.setValue(value)


def _save_assignment_rules(model, sbml_model):
    for p_id, formula in model.assignment_rules.items():
        rule = sbml_model.createAssignmentRule()
        rule.setVariable(p_id)
        rule.setFormula(formula)
        sbml_model.getParameter(p_id).setConstant(False)


def _save_metadata(elem, sbml_elem):
    if elem.metadata:
        notes = ['<p>{}: {}</p>'.format(key, value) for key, value in elem.metadata.items()]
        note_string = '<html>' + ''.join(notes) + '</html>'
        note_xml = XMLNode.convertStringToXMLNode(note_string)
        note_xml.getNamespaces().add('http://www.w3.org/1999/xhtml')
        sbml_elem.setNotes(note_xml)


def _load_metadata(sbml_elem, elem):
    notes = sbml_elem.getNotes()

    if notes:
        html_tag = notes.getChild(0)
        for i in range(html_tag.getNumChildren()):
            child = html_tag.getChild(i)
            note_str = child.getChild(0).getCharacters()
            if ':' in note_str:
                key, value = note_str.split(':', 1)
                elem.metadata[key.strip()] = value.strip()


