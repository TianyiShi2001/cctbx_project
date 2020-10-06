
"""
Module for managing and manipulating all properties (and underlying objects)
associated with an atomic (or pseudo-atomic) model, including both basic
attributes stored in a PDB file, scattering information, and geometry
restraints.
"""

from __future__ import absolute_import, division, print_function

from libtbx.test_utils import approx_equal
from libtbx.utils import Sorry, user_plus_sys_time, null_out
from libtbx import group_args, str_utils

import iotbx.pdb
import iotbx.cif.model
import iotbx.ncs
from iotbx.pdb.amino_acid_codes import one_letter_given_three_letter
# from iotbx.pdb.atom_selection import AtomSelectionError
from iotbx.pdb.misc_records_output import link_record_output
from iotbx.cif import category_sort_function

from cctbx.array_family import flex
from cctbx import xray
from cctbx import adptbx
from cctbx import geometry_restraints
from cctbx import adp_restraints
from cctbx import crystal

import mmtbx.restraints
import mmtbx.hydrogens
from mmtbx.hydrogens import riding
import mmtbx.model.statistics
import mmtbx.monomer_library.server
from mmtbx.geometry_restraints.torsion_restraints.utils import check_for_internal_chain_ter_records
import mmtbx.tls.tools as tls_tools
from mmtbx import ias
from mmtbx import utils
from mmtbx import ncs
from mmtbx.ncs.ncs_utils import apply_transforms
from mmtbx.command_line import find_tls_groups
from mmtbx.monomer_library.pdb_interpretation import grand_master_phil_str
from mmtbx.geometry_restraints.torsion_restraints.reference_model import \
    add_reference_dihedral_restraints_if_requested, reference_model_str, reference_model
from mmtbx.geometry_restraints.torsion_restraints.torsion_ncs import torsion_ncs
from mmtbx.refinement import print_statistics
from mmtbx.refinement import anomalous_scatterer_groups
from mmtbx.refinement import geometry_minimization
import cctbx.geometry_restraints.nonbonded_overlaps as nbo

from scitbx import matrix
from iotbx.bioinformatics import sequence
from mmtbx.validation.sequence import master_phil as sequence_master_phil
from mmtbx.validation.sequence import validation as sequence_validation

import boost_adaptbx.boost.python as bp
import six
from six.moves import zip
from six.moves import range

ext = bp.import_ext("mmtbx_validation_ramachandran_ext")
from mmtbx_validation_ramachandran_ext import rama_eval
from mmtbx.rotamer.rotamer_eval import RotamerEval
from mmtbx.rotamer.rotamer_eval import RotamerID

from mmtbx.geometry_restraints import ramachandran

ext2 = bp.import_ext("iotbx_pdb_hierarchy_ext")
from iotbx_pdb_hierarchy_ext import *

from six.moves import cStringIO as StringIO
from copy import deepcopy
import sys
import math

time_model_show = 0.0

def find_common_water_resseq_max(pdb_hierarchy):
  get_class = iotbx.pdb.common_residue_names_get_class
  hy36decode = iotbx.pdb.hy36decode
  result = None
  for model in pdb_hierarchy.models():
    for chain in model.chains():
      for rg in chain.residue_groups():
        for ag in rg.atom_groups():
          if (get_class(name=ag.resname) == "common_water"):
            try: i = hy36decode(width=4, s=rg.resseq)
            except (RuntimeError, ValueError): pass
            else:
              if (result is None or result < i):
                result = i
            break
  return result

class xh_connectivity_table(object):
  # XXX need angle information as well
  def __init__(self, geometry, xray_structure):
    bond_proxies_simple, asu = geometry.geometry.get_all_bond_proxies(
        sites_cart=xray_structure.sites_cart())
    scatterers = xray_structure.scatterers()
    self.table = []
    for proxy in bond_proxies_simple:
      i_seq, j_seq = proxy.i_seqs
      i_x, i_h = None, None
      if(scatterers[i_seq].element_symbol() in ["H", "D"]):
        i_h = i_seq
        i_x = j_seq
        site_x = scatterers[i_x].site
        site_h = scatterers[i_h].site
        const_vect = flex.double(site_h)-flex.double(site_x)
        distance_model = xray_structure.unit_cell().distance(site_x, site_h)
        self.table.append([i_x, i_h, const_vect, proxy.distance_ideal,
                           distance_model])
      if(scatterers[j_seq].element_symbol() in ["H", "D"]):
        i_h = j_seq
        i_x = i_seq
        site_x = scatterers[i_x].site
        site_h = scatterers[i_h].site
        const_vect = flex.double(site_h)-flex.double(site_x)
        distance_model = xray_structure.unit_cell().distance(site_x, site_h)
        self.table.append([i_x, i_h, const_vect, proxy.distance_ideal,
                           distance_model])

class xh_connectivity_table2(object):
  def __init__(self, geometry, xray_structure):
    bond_proxies_simple, asu = geometry.geometry.get_all_bond_proxies(
        sites_cart=xray_structure.sites_cart())
    scatterers = xray_structure.scatterers()
    self.table = {}
    for proxy in bond_proxies_simple:
      i_seq, j_seq = proxy.i_seqs
      i_x, i_h = None, None
      if(scatterers[i_seq].element_symbol().upper() in ["H", "D"]):
        i_h = i_seq
        i_x = j_seq
      if(scatterers[j_seq].element_symbol().upper() in ["H", "D"]):
        i_h = j_seq
        i_x = i_seq
      if([i_x, i_h].count(None)==0):
        site_x = scatterers[i_x].site
        site_h = scatterers[i_h].site
        const_vect = flex.double(site_h)-flex.double(site_x)
        distance_model = xray_structure.unit_cell().distance(site_x, site_h)
        self.table.setdefault(i_h, []).append([i_x, i_h, const_vect,
          proxy.distance_ideal, distance_model])
    for p in geometry.geometry.angle_proxies:
      k,l,m = p.i_seqs
      els = [scatterers[k].element_symbol().upper(),
             scatterers[l].element_symbol().upper(),
             scatterers[m].element_symbol().upper()]
      o = flex.double()
      h = []
      ih=None
      if(els.count("H")<2 and els.count("D")<2):
        for i in p.i_seqs:
          s = scatterers[i]
          o.append(s.occupancy)
          sct = s.scattering_type.strip().upper()
          h.append(sct)
          if(sct in ["H","D"]): ih = i
        if("H" in h and not o.all_eq(o[0])):
          self.table.setdefault(ih).append(p.i_seqs)

class manager(object):
  """
  Wrapper class for storing and manipulating an iotbx.pdb.hierarchy object and
  a cctbx.xray.structure object, plus optional restraints-related objects.
  Being refactored to handle all model needs.
  Refactoring roadmap:
  1. Be able to create it in a new way (from model_input object) and keep
    everything else working. This constraints changing names of variables.
  2. Use it in some small places.
  3. Use it in phenix.refine and make sure it is really working
  4. Check all places where it is used and convert them to new way of creating it.
  5. Start refactoring with phenix.refine to remove unnecessary things.
  """

  def __init__(self,
      model_input,
      pdb_hierarchy = None,  # To create model from hierarchy. Makes sence only in model-building when hierarchy created from nothing
      crystal_symmetry = None,
      restraint_objects = None, # ligand restraints in cif format
      monomer_parameters = None, # mmtbx.utils.cif_params scope # temporarily for phenix.refine
      pdb_interpretation_params = None,
      process_input = False, # obtain processed_pdb_file straight away
      build_grm = False,  # build GRM straight away, without waiting for get_restraints_manager() call
      stop_for_unknowns = True,
      log = None,
      expand_with_mtrix = True):

    self._xray_structure    = None
    self.tls_groups         = None
    self.restraints_manager = None
    self._pdb_hierarchy = pdb_hierarchy
    self._model_input = model_input
    self._restraint_objects = restraint_objects
    self._monomer_parameters = monomer_parameters
    self._pdb_interpretation_params = None
    self.set_pdb_interpretation_params(pdb_interpretation_params)
    # Important! if shift_cart is not None, model_input - in original coords,
    # self.get_hierarchy(), self.get_xray_structure, self.get_sites_cart - in shifted coords.
    # self.crystal_symmetry() - shifted (boxed) one
    # If shift_cart is None - everything is consistent.
    self._shift_cart = None # shift of this model since original location
    self._unit_cell_crystal_symmetry = None # original crystal symmetry

    self._stop_for_unknowns = stop_for_unknowns
    self._processed_pdb_file = None
    self._processed_pdb_files_srv = None
    self._crystal_symmetry = crystal_symmetry
    self.refinement_flags = None
    # IAS related, need some cleaning!
    self.ias_manager = None
    self.use_ias = False    # remove later, use presence of ias_manager
                          # somewhat challenging because of ias.build_only parameter in phenix.refine
    self._ncs_groups = None
    self._anomalous_scatterer_groups = []
    self.log = log
    self.exchangable_hd_groups = []
    self.original_xh_lengths = None
    self.riding_h_manager = None
    # for reprocessing. Probably select() will also use...
    self.scattering_dict_info = None

    self._ss_annotation = None
    if(self._model_input is not None):
      self._ss_annotation = self._model_input.extract_secondary_structure()


    self.link_records_in_pdb_format = None # Nigel's stuff up to refactoring
    self.all_chain_proxies = None # probably will keep it. Need to investigate
          # 'smart' selections provided by it. If they useless, remove.

    # These are new
    self.xray_scattering_dict = None    # 2020: leave only one: self._scattering_dict
    self.neutron_scattering_dict = None # 2020: leave only one: self._scattering_dict
    self._has_hd = None
    self.model_statistics_info = None
    self._master_sel = flex.size_t([]) # selection of master part if NCS constraints are in use
    self._atom_selection_cache = None
    self._ncs_obj = None
    self._mon_lib_srv = None
    self._ener_lib = None
    self._rotamer_eval = None
    self._rotamer_id = None
    self._rama_eval = None
    self._original_model_format = None
    self._ss_manager = None
    self._site_symmetry_table = None

    self._ter_indices = None
    if(self._model_input is not None):
      self._ter_indices = self._model_input.ter_indices()

    # sequence data objects
    self._sequence_validation = None

    # here we start to extract and fill appropriate field one by one
    # depending on what's available.
    if self._model_input is not None:
      s = str(type(model_input))
      if s.find("cif") > 0:
        self._original_model_format = "mmcif"
      elif s.find("pdb") > 0:
        self._original_model_format = "pdb"
      # input xray_structure most likely don't have proper crystal symmetry
      if self.crystal_symmetry() is None:
        inp_cs = self._model_input.crystal_symmetry()
        if inp_cs and not inp_cs.is_empty() and not inp_cs.is_nonsense():
          self._crystal_symmetry = inp_cs
    # Handle MTRIX
    self.mtrix_operators = None
    if(self._model_input is not None):
      self.mtrix_operators = self._model_input.process_MTRIX_records()
    self._mtrix_expanded = False
    if(expand_with_mtrix):
      self.expand_with_MTRIX_records()
    # Handle BIOMT
    self.biomt_operators = None
    if(self._model_input is not None):
      # XXX FIX LATER
      try:
        self.biomt_operators = self._model_input.process_BIOMT_records()
      except: pass
      # XXX FIX LATER

    self._biomt_expanded = False

    self._clash_guard_msg = None

    if process_input or build_grm:
      assert self._processed_pdb_file is None
      assert self.all_chain_proxies is None
      self.process_input_model(make_restraints = build_grm)

    # do pdb_hierarchy
    if self._pdb_hierarchy is None: # got nothing in parameters
      if self._processed_pdb_file is not None:
        self.all_chain_proxies = self._processed_pdb_file.all_chain_proxies
        self._pdb_hierarchy = self.all_chain_proxies.pdb_hierarchy
      elif self._model_input is not None:
        # self._pdb_hierarchy = deepcopy(self._model_input).construct_hierarchy()
        self._pdb_hierarchy = deepcopy(self._model_input).construct_hierarchy(
            self._pdb_interpretation_params.pdb_interpretation.sort_atoms)
        # Perform the flipping of symmertric amino acids - swaps the coordinates
        if(self._pdb_interpretation_params.pdb_interpretation.flip_symmetric_amino_acids):
          self._pdb_hierarchy.flip_symmetric_amino_acids()
    # Move this away from constructor
    self._update_atom_selection_cache()
    self.get_hierarchy().atoms().reset_i_seq()

    if not self.all_chain_proxies and self._processed_pdb_file:
      self.all_chain_proxies = self._processed_pdb_file.all_chain_proxies

    # do xray_structure
    if self._xray_structure is not None:
      if self.crystal_symmetry() is None:
        self._crystal_symmetry = self._xray_structure.crystal_symmetry()

    if self._xray_structure is not None:
      assert self._xray_structure.scatterers().size() == self._pdb_hierarchy.atoms_size()

  @classmethod
  def from_sites_cart(cls,
      sites_cart,
      atom_name=' CA ',
      resname='GLY',
      chain_id='A',
      b_iso=30.,
      b_iso_list=None,
      occ=1.,
      count=0,
      occ_list=None,
      scatterer='C',
      crystal_symmetry=None):
    assert sites_cart is not None
    hierarchy = iotbx.pdb.hierarchy.root()
    m = iotbx.pdb.hierarchy.model()
    c = iotbx.pdb.hierarchy.chain()
    c.id=chain_id
    hierarchy.append_model(m)
    m.append_chain(c)
    if not b_iso_list:
      b_iso_list=sites_cart.size()*[b_iso]
    if not occ_list:
      occ_list=sites_cart.size()*[occ]
    assert len(occ_list) == len(b_iso_list) == sites_cart.size()
    for sc, b_iso, occ in zip(sites_cart,b_iso_list,occ_list):
      count+=1
      rg=iotbx.pdb.hierarchy.residue_group()
      c.append_residue_group(rg)
      ag=iotbx.pdb.hierarchy.atom_group()
      rg.append_atom_group(ag)
      a=iotbx.pdb.hierarchy.atom()
      ag.append_atom(a)
      rg.resseq = iotbx.pdb.resseq_encode(count)
      ag.resname=resname
      a.set_b(b_iso)
      a.set_element(scatterer)
      a.set_occ(occ)
      a.set_name(atom_name)
      a.set_xyz(sc)
      a.set_serial(count)
    return cls(model_input = None, pdb_hierarchy=hierarchy,
       crystal_symmetry=crystal_symmetry)

  @staticmethod
  def get_default_pdb_interpretation_scope():
    """
    Get parsed parameters (libtbx.phil.scope). Use this function to
    avoid importing pdb_interpretation phil strings and think about how to
    parse it. Does not need the instance of class (staticmethod).
    Then modify what needed to be modified and init this class normally.
    """
    from mmtbx.geometry_restraints.external import external_energy_params_str
    return iotbx.phil.parse(
          input_string = grand_master_phil_str +\
                         reference_model_str +\
                         external_energy_params_str,
          process_includes=True)

  @staticmethod
  def get_default_pdb_interpretation_params():
    """
    Get the default extract object (libtbx.phil.scope_extract)
    """
    return manager.get_default_pdb_interpretation_scope().extract()

  def get_current_pdb_interpretation_params(self):
    """
    Get the current extract object (libtbx.phil.scope_extract)
    """
    return self._pdb_interpretation_params


  def get_xray_structure(self):
    if(self._xray_structure is None):
      cs = self.crystal_symmetry()
      assert cs is not None
      assert cs.unit_cell() is not None
      assert cs.space_group() is not None
      self._xray_structure = self.get_hierarchy().extract_xray_structure(
        crystal_symmetry = cs)
    return self._xray_structure

  # Setters

  def set_sites_cart(self, sites_cart, selection=None):
    if(sites_cart is None): return
    assert isinstance(sites_cart, flex.vec3_double)
    if(selection is not None):
      sites_cart_ = self.get_hierarchy().atoms().extract_xyz()
      sites_cart_ = sites_cart_.set_selected(selection, sites_cart)
    else:
      sites_cart_ = sites_cart
    self.get_hierarchy().atoms().set_xyz(sites_cart_)
    if(self._xray_structure is not None):
      self._xray_structure.set_sites_cart(sites_cart_)

  def set_b_iso(self, values, selection=None):
    if(values is None): return
    if(selection is not None):
      b_iso = self.get_hierarchy().atoms().extract_b()
      b_iso = b_iso.set_selected(selection, values)
    else:
      b_iso = values
    if(self._xray_structure is not None):
      self.get_xray_structure().set_b_iso(values = b_iso, selection=selection)
    self.get_hierarchy().atoms().set_b(b_iso)

  def set_occupancies(self, values, selection=None):
    if(values is None): return
    if(selection is not None):
      occ = self.get_hierarchy().atoms().extract_occ()
      occ = occ.set_selected(selection, values)
    else:
      occ = values
    if(self._xray_structure is not None):
      self.get_xray_structure().set_occupancies(value = occ)
    self.get_hierarchy().atoms().set_occ(occ)

  # Getters

  def get_b_iso(self):
    return self.get_hierarchy().atoms().extract_b()

  def get_occ(self):
    return self.get_hierarchy().atoms().extract_occ()

  def get_sites_cart(self):
    return self.get_hierarchy().atoms().extract_xyz()

  def get_sites_frac(self):
    return self.get_xray_structure().sites_frac()

  def get_atoms(self):
    return self.get_hierarchy().atoms()

  def processed(self):
    fl = self._processed_pdb_file is not None or \
         self.all_chain_proxies   is not None or \
         self._xray_structure     is not None or \
         self.restraints_manager  is not None
    return fl

  def set_log(self, log):
    self.log = log

  def __repr__(self):
    """
      Summarize the model_manager
    """
    h = self.get_hierarchy()
    if h:
      counts = h.overall_counts()
      nres = counts.n_residues
      nchains = counts.n_chains
    else:
      nres = 0
      nchains = 0
    if self.shift_cart():
      sc = tuple(self.shift_cart())
    else:
      sc = (0, 0, 0)
    return "Model manager "+\
      "\n%s\nChains: %s Residues %s \nWorking coordinate shift %s)" %(
      str(self.unit_cell_crystal_symmetry()).replace("\n"," "),
      str(nchains),
      str(nres),
      str(sc))

  def set_stop_for_unknowns(self, value):
    self._stop_for_unknowns=value

  def get_stop_for_unknowns(self):
    return self._stop_for_unknowns

  def set_pdb_interpretation_params(self, params):
    #
    # Consider invalidating self.restraints_manager here, because it could be already
    # constructed with different params. Done.
    #
    # check if we got only inside of pdb_interpretation scope.
    # For mmtbx.command_line.load_model_and_data
    if params is None:
      self._pdb_interpretation_params = manager.get_default_pdb_interpretation_params()
    else:
      # if getattr(params, "sort_atoms", None) is not None:
      full_params = manager.get_default_pdb_interpretation_params()
      if getattr(params, "sort_atoms", None) is not None:
        full_params.pdb_interpretation = params
        assert 0, "This is not supported anymore. Pass whatever you got" + \
            " from get_default_pdb_interpretation_params()"
      if hasattr(params, "pdb_interpretation"):
        full_params.pdb_interpretation = params.pdb_interpretation
      if hasattr(params, "geometry_restraints"):
        full_params.geometry_restraints = params.geometry_restraints
      if hasattr(params, "reference_model"):
        full_params.reference_model = params.reference_model
      for attr in ['amber', 'schrodinger']:
        if hasattr(params, attr):
          setattr(full_params, attr, getattr(params, attr))
      self._pdb_interpretation_params = full_params
    self.unset_restraints_manager()

  def set_nonbonded_weight(self, value):
    params = self.get_current_pdb_interpretation_params()
    params.pdb_interpretation.nonbonded_weight = value
    self.set_pdb_interpretation_params(params = params)

  def check_consistency(self):
    """
    Primarilly for debugging
    """
    s1 = self.get_hierarchy().atoms().extract_xyz()
    s2 = self.get_xray_structure().sites_cart()
    d = flex.sqrt((s1 - s2).dot())
    assert d<1.e-4

  def set_ramachandran_plot_restraints(self, rama_params):
    """ rama_params - mmtbx.geometry_restraints.ramachandran.master_phil->
        ramachandran_plot_restraints"""
    self.unset_ramachandran_plot_restraints()
    grm = self.get_restraints_manager().geometry
    ramachandran_restraints_manager = ramachandran.ramachandran_manager(
      pdb_hierarchy  = self.get_hierarchy(),
      params         = rama_params,
      log            = self.log)
    grm.set_ramachandran_restraints(manager = ramachandran_restraints_manager)

  def unset_ramachandran_plot_restraints(self):
    grm = self.get_restraints_manager().geometry
    grm.remove_ramachandran_in_place()

  def crystal_symmetry(self):
    cs = self._crystal_symmetry
    if(cs is None or cs.is_empty() or cs.is_nonsense()):
      return None
    return cs

  def get_restraint_objects(self):
    return self._restraint_objects

  def set_restraint_objects(self, restraint_objects):
    self._restraint_objects = restraint_objects
    self.unset_restraints_manager()
    self._processed_pdb_files_srv = None
    self.all_chain_proxies = None

  def get_monomer_parameters(self):
    return self._monomer_parameters

  def get_ss_annotation(self, log=null_out()):
    return self._ss_annotation

  def set_ss_annotation(self, ann):
    self._ss_annotation = ann

  def set_unit_cell_crystal_symmetry_and_shift_cart(self,
       unit_cell_crystal_symmetry=None,
       shift_cart=None):
    '''
      Set up record of shift_cart (shift since unit_cell location) and
      unit_cell_crystal_symmetry

      Normally used to set up info with zero shift_cart and
      unit_cell_crystal_symmetry equal to crystal_symmetry
    '''
    assert self.shift_cart() is None
    if not shift_cart:
      shift_cart = (0,0,0)
      if not unit_cell_crystal_symmetry:
        unit_cell_crystal_symmetry = self.crystal_symmetry()

    self._shift_cart = shift_cart
    self._unit_cell_crystal_symmetry=unit_cell_crystal_symmetry

  def shift_model_and_set_crystal_symmetry(self,
       shift_cart,     # shift to apply
       crystal_symmetry = None, # optional new crystal symmetry
       ):

    '''
      Method to apply a coordinate shift to a model object and keep track of
      it self._shift_cart

      NOTE: Normally use this method along with shift_model_back() to
      shift the coordinates of the model

      Sets the new crystal_symmetry
      Maintains any existing unit_cell_crystal_symmetry

      Takes into account any previous shifts by looking at existing
      shift_cart and unit_cell_crystal_symmetry
    '''
    # checks
    assert shift_cart is not None
    assert len(list(shift_cart)) == 3
    assert (crystal_symmetry is None) or isinstance(
      crystal_symmetry,  crystal.symmetry)

    # Get shift info  that knows about unit_cell_crystal_symmetry
    #   and any prevous shift_cart

    unit_cell_crystal_symmetry = self.unit_cell_crystal_symmetry()
    if not unit_cell_crystal_symmetry:
      unit_cell_crystal_symmetry = self.crystal_symmetry()

    if self._shift_cart is not None:
       original_shift_cart = self._shift_cart
    else:
       original_shift_cart = (0, 0, 0)

    # Get the new crystal symmetry to apply
    if not crystal_symmetry:
      crystal_symmetry = self.crystal_symmetry()

    # Get the total shift since original
    total_shift=[sm_sc+sc for sm_sc,sc in zip(original_shift_cart,shift_cart)]

    # New coordinates after applying shift_cart
    sites_cart_new = self.get_sites_cart() + shift_cart

    #Set symmetry and sites_cart
    self.set_crystal_symmetry_and_sites_cart(crystal_symmetry, sites_cart_new)

    # Set up new shift info with updated shift_cart,

    # NOTE: sm.shift_cart is now the total shift since original position of this
    #   model, it is not the shift in this step alone. It is the shift which
    #   reversed will put the model back where it belongs

    self._shift_cart = total_shift
    self._unit_cell_crystal_symmetry =  unit_cell_crystal_symmetry

  def shift_model_back(self):
    '''
      Shift the model back to its original position and restore original
      crystal_symmetry.

      Normally use this method to shift coordinates back to original position
      if needed (in most cases this is not necessary because the model
      is automatically shifted back when it is written out).

      Requires that shifts have been set up
    '''
    assert self.shift_cart() is not None
    shift_cart_to_apply=tuple([-x for x in self.shift_cart()])  # Shift to apply

    self.shift_model_and_set_crystal_symmetry(
     shift_cart = shift_cart_to_apply,
     crystal_symmetry = self._unit_cell_crystal_symmetry)


  def set_unit_cell_crystal_symmetry(self, crystal_symmetry):
    '''
      Set the unit_cell_crystal_symmetry (original crystal symmetry)

      Only used to reset original crystal symmetry of model
      Requires that there is no shift_cart for this model in
    '''
    assert crystal_symmetry is not None

    if self._shift_cart is None:
      self._shift_cart = (0, 0, 0)
    else:
      assert self._shift_cart == (0 ,0 ,0)

    self._unit_cell_crystal_symmetry = crystal_symmetry

  def set_crystal_symmetry(self, crystal_symmetry):
    '''
      Set the crystal_symmetry, keeping sites_cart the same

      NOTE: Normally instead use
        shift_model_and_set_crystal_symmetry(shift_cart=shift_cart) and
      shift_model_back() to shift the coordinates of the model.

      Uses set_crystal_symmetry_and_sites_cart because sites_cart have to
      be replaced in either case.
    '''
    if(not self.processed()):
      self._crystal_symmetry = crystal_symmetry
    else:
      self.set_crystal_symmetry_and_sites_cart(crystal_symmetry,None)

  def set_crystal_symmetry_and_sites_cart(self, crystal_symmetry, sites_cart):

    '''
      Set the crystal symmetry and then replace sites_cart with supplied sites

      NOTE: Normally instead use
        shift_model_and_set_crystal_symmetry(shift_cart=shift_cart) and
      shift_model_back() to shift the coordinates of the model.

      If there is no xray_structure and no sites_cart are supplied this
      sets self._crystal_symmetry and updates. This is used to set
      crystal_symmetry in a model that has coordinates but no crystal_symmetry

      If existing xray_structure and xray_structure.crystal_symmetry is similar
       to supplied crystal_symmetry, just replace sites_cart and update

      If existing xray_structure and supplied crystal_symmetry is new, make a
      new xray_structure and put in the supplied sites_cart and update. If no
      sites_cart, use existing.

    '''

    assert crystal_symmetry is not None  # must supply crystal_symmetry

    if(self.crystal_symmetry() is None):
      # Set self._crystal_symmetry.
      assert self._xray_structure is None # can't have xrs without crystal sym
      self._crystal_symmetry = crystal_symmetry

    if self.crystal_symmetry().is_similar_symmetry(crystal_symmetry):
      # Keep the xray_structure but change sites_cart if present and update
      xrs=self.get_xray_structure() # Make sure xrs is set up

      # make self._crystal_symmetry identical to xrs crystal_symmetry
      self._crystal_symmetry = xrs.crystal_symmetry()

      # set the sites_cart if supplied
      self.set_sites_cart(sites_cart = sites_cart)
      self._update_has_hd()

    else:  # Make a new xray_structure with new symmetry and put in sites

      xrs=self.get_xray_structure() # Make sure xrs is set up

      if sites_cart is None:
        sites_cart=xrs.sites_cart()

      # Changing crystal_symmetry changes sites_frac but keeps sites_cart same

      # Reset _crystal_symmetry
      scattering_table = xrs.scattering_type_registry().last_table()
      scatterers = xrs.scatterers()
      sp = crystal.special_position_settings(crystal_symmetry)
      self._xray_structure = xray.structure(sp, scatterers)
      self._crystal_symmetry = \
        self._xray_structure.crystal_symmetry() # make it identical
      self.set_sites_cart(sites_cart = sites_cart)

      if scattering_table: # if not there, were not any scattering tables before
        self.setup_scattering_dictionaries(scattering_table = scattering_table)
      # GRM is not valid if the symmetry is changed
      self.unset_restraints_manager()

  def unit_cell_crystal_symmetry(self):
    if self._unit_cell_crystal_symmetry is not None:
      return self._unit_cell_crystal_symmetry

    else:
      return None

  def shift_cart(self):
    '''
      Return the value of the shift_cart, if available.
    '''
    if self._shift_cart:
      return self._shift_cart
    else:
      return None

  def set_shift_cart(self,shift_cart):
    '''
      Change the value of the recorded coordinate shift applied to a model
      without changing anything about the model.  This effectively changes
        the output shift that is going to be applied to this model when it is
             written out.

      Also for backwards compatibility make sure
      self._unit_cell_crystal_symmetry is set if self._crystal_symmetry is set

      NOTE: Normally instead use
        shift_model_and_set_crystal_symmetry(shift_cart=shift_cart) and
      shift_model_back() to shift the coordinates of the model.

    '''
    self._shift_cart = shift_cart
    if not self._unit_cell_crystal_symmetry: # set  _unit_cell_crystal_symmetry
      self._unit_cell_crystal_symmetry = self.crystal_symmetry()

  def _shift_back(self, pdb_hierarchy):
    assert pdb_hierarchy is not None
    sites_cart = pdb_hierarchy.atoms().extract_xyz()
    shift_back = [-self._shift_cart[0], -self._shift_cart[1],
        -self._shift_cart[2]]
    sites_cart_shifted = sites_cart+\
      flex.vec3_double(sites_cart.size(), shift_back)
    pdb_hierarchy.atoms().set_xyz(sites_cart_shifted)

  def set_refinement_flags(self, flags):
    self.refinement_flags = flags

  def get_number_of_atoms(self):
    return self.get_hierarchy().atoms().size()

  def size(self):
    return self.get_number_of_atoms()

  def get_atom_selection_cache(self):
    if self._atom_selection_cache is None:
      self._update_atom_selection_cache()
    return self._atom_selection_cache

  def get_number_of_models(self):
    return len(self.get_hierarchy().models())

  def get_site_symmetry_table(self):
    if self._site_symmetry_table is not None:
      return self._site_symmetry_table
    elif self.all_chain_proxies is not None:
      self._site_symmetry_table = self.all_chain_proxies.site_symmetry_table()
    return self._site_symmetry_table

  def initialize_anomalous_scatterer_groups(
      self,
      find_automatically=True,
      groups_from_params=None):
    self.n_anomalous_total = 0
    result = []
    if find_automatically:
      result = anomalous_scatterer_groups.find_anomalous_scatterer_groups(
        pdb_atoms=self.get_hierarchy().atoms(),
        xray_structure=self.get_xray_structure(),
        group_same_element=False,
        out=self.log)
      for group in result:
        self.n_anomalous_total += group.iselection.size()
    else:
      if len(groups_from_params) != 0:
        chain_proxies = self.all_chain_proxies
        sel_cache = self.get_atom_selection_cache()
        for group in groups_from_params:
          if (group.f_prime is None): group.f_prime = 0
          if (group.f_double_prime is None): group.f_double_prime = 0
          aag = xray.anomalous_scatterer_group(
            iselection=chain_proxies.phil_atom_selection(
              cache=sel_cache,
              scope_extract=group,
              attr="selection",
              raise_if_empty_selection=True).iselection(),
            f_prime=group.f_prime,
            f_double_prime=group.f_double_prime,
            refine=group.refine,
            selection_string=group.selection)
          # aag.show_summary(out=log)
          result.append(aag)
          self.n_anomalous_total += aag.iselection.size()
    self._anomalous_scatterer_groups = result
    for group in self._anomalous_scatterer_groups:
      group.copy_to_scatterers_in_place(scatterers=self._xray_structure.scatterers())
    return self._anomalous_scatterer_groups, self.n_anomalous_total

  def set_anomalous_scatterer_groups(self, groups):
    self._anomalous_scatterer_groups = groups
    new_n_anom_total = 0
    for g in self._anomalous_scatterer_groups:
      new_n_anom_total += g.iselection.size()
    self.n_anomalous_total = new_n_anom_total

  def get_anomalous_scatterer_groups(self):
    return self._anomalous_scatterer_groups

  def have_anomalous_scatterer_groups(self):
    return (self._anomalous_scatterer_groups is not None and
        len(self._anomalous_scatterer_groups) > 0)

  def update_anomalous_groups(self, out=sys.stdout):
    if self.have_anomalous_scatterer_groups():
      modified = False
      sel_cache = self.get_atom_selection_cache()
      for i_group, group in enumerate(self._anomalous_scatterer_groups):
        if (group.update_from_selection):
          isel = sel_cache.selection(group.selection_string).iselection()
          assert (len(isel) == len(group.iselection))
          if (not isel.all_eq(group.iselection)):
            print("Updating %d atom(s) in anomalous group %d" % \
              (len(isel), i_group+1), file=out)
            print("  selection string: %s" % group.selection_string, file=out)
            group.iselection = isel
            modified = True
      return modified

  def anomalous_scatterer_groups_as_pdb(self):
    out = StringIO()
    if self.have_anomalous_scatterer_groups():
      pr = "REMARK   3  "
      print(pr+"ANOMALOUS SCATTERER GROUPS DETAILS.", file=out)
      print(pr+" NUMBER OF ANOMALOUS SCATTERER GROUPS : %-6d"%\
        len(self._anomalous_scatterer_groups), file=out)
      counter = 0
      for group in self._anomalous_scatterer_groups:
        counter += 1
        print(pr+" ANOMALOUS SCATTERER GROUP : %-6d"%counter, file=out)
        lines = str_utils.line_breaker(group.selection_string, width=45)
        for i_line, line in enumerate(lines):
          if(i_line == 0):
            print(pr+"  SELECTION: %s"%line, file=out)
          else:
            print(pr+"           : %s"%line, file=out)
        print(pr+"  fp  : %-15.4f"%group.f_prime, file=out)
        print(pr+"  fdp : %-15.4f"%group.f_double_prime, file=out)
    return out.getvalue()

  def restraints_manager_available(self):
    return self.restraints_manager is not None

  def get_restraints_manager(self):
    return self.restraints_manager

  def set_non_unit_occupancy_implies_min_distance_sym_equiv_zero(self,value):
    if self._xray_structure is not None:
      self._xray_structure.set_non_unit_occupancy_implies_min_distance_sym_equiv_zero(value)
      self.set_xray_structure(self._xray_structure.customized_copy(
          non_unit_occupancy_implies_min_distance_sym_equiv_zero=value))

  def get_hd_selection(self):
    xrs = self.get_xray_structure()
    return xrs.hd_selection()

  def get_ias_selection(self):
    if self.ias_manager is None:
      return None
    else:
      return self.ias_manager.get_ias_selection()

  def apply_selection_string(self, selection_string):
    if not selection_string:
      return
    sel = self.selection(selection_string)
    return self.select(sel)

  def selection(self, string, optional=True):
    if self.all_chain_proxies is None:
      return self.get_atom_selection_cache().selection(string, optional=optional)
    else:
      return self.all_chain_proxies.selection(
          string,
          cache=self._atom_selection_cache,
          optional=optional)

  def iselection(self, string):
    result = self.selection(string)
    if result is None:
      return None
    return result.iselection()

  def sel_backbone(self):
    assert self.all_chain_proxies is not None
    return self.all_chain_proxies.sel_backbone_or_sidechain(True, False)

  def sel_sidechain(self):
    assert self.all_chain_proxies is not None
    return self.all_chain_proxies.sel_backbone_or_sidechain(False, True)

  def set_xray_structure(self, xray_structure):
    # XXX Delete as a method or make sure all TLS, NCS, refinement flags etc
    # XXX are still consistent!
    same_symmetry = True
    if(self._xray_structure is not None):
      same_symmetry = self._xray_structure.crystal_symmetry().is_similar_symmetry(
        xray_structure.crystal_symmetry())
    if(not same_symmetry):
      self.unset_restraints_manager()
      self.set_crystal_symmetry(
        crystal_symmetry = xray_structure.crystal_symmetry())
    self._xray_structure = xray_structure
    self.get_hierarchy().adopt_xray_structure(self._xray_structure)
    self._update_has_hd()

  def get_mon_lib_srv(self):
    if self._mon_lib_srv is None:
      self._mon_lib_srv = mmtbx.monomer_library.server.server()
    return self._mon_lib_srv

  def get_ener_lib(self):
    if self._ener_lib is None:
      self._ener_lib = mmtbx.monomer_library.server.ener_lib()
    return self._ener_lib

  def rotamer_outlier_selection(self):
    rm = self.get_rotamer_manager()
    result = flex.bool(self.size(), False)
    for model in self.get_hierarchy().models():
      for chain in model.chains():
        for residue_group in chain.residue_groups():
          for conformer in residue_group.conformers():
            for residue in conformer.residues():
               if(rm.evaluate_residue(residue)=="OUTLIER"):
                 sel = residue.atoms().extract_i_seq()
                 result = result.set_selected(sel, True)
    return result

  def get_rotamer_manager(self):
    if self._rotamer_eval is None:
      self._rotamer_eval = RotamerEval(mon_lib_srv=self.get_mon_lib_srv())
    return self._rotamer_eval

  def get_rotamer_id(self):
    if self._rotamer_id is None:
      self._rotamer_id = RotamerID()
    return self._rotamer_id

  def get_ramachandran_manager(self):
    if self._rama_eval is None:
      self._rama_eval = rama_eval()
    return self._rama_eval

  def get_apply_cif_links(self):
    if self.all_chain_proxies is not None:
      return self.all_chain_proxies.apply_cif_links
    return []

  def update_xrs(self, hierarchy=None):
    """
    Updates xray structure using self._pdb_hierarchy for cases when it
    was modified outside. E.g. refinement, minimization, etc.
    """
    if hierarchy is not None:
      # !!! This could fail even for very similar hierarchies.
      # see example in
      # cctbx_project/mmtbx/secondary_structure/build/tst_1.py
      assert hierarchy.is_similar_hierarchy(other=self._pdb_hierarchy)
      self._pdb_hierarchy = hierarchy
    self._xray_structure = self._pdb_hierarchy.extract_xray_structure(
        crystal_symmetry=self.crystal_symmetry())

  def _figure_out_cs_to_output(self, do_not_shift_back, output_cs):
    if not output_cs:
      return None
    if do_not_shift_back:
      return self._crystal_symmetry
    else:
      if self._shift_cart is not None:
        return self.unit_cell_crystal_symmetry()
      else:
        return self.crystal_symmetry()

  def _figure_out_hierarchy_to_output(self, do_not_shift_back):
    hierarchy_to_output = self.get_hierarchy()
    if hierarchy_to_output is not None:
      hierarchy_to_output = hierarchy_to_output.deep_copy()
    if (self._shift_cart is not None) and (not do_not_shift_back):
      self._shift_back(hierarchy_to_output)
    return hierarchy_to_output

  def model_as_pdb(self,
      output_cs = True,
      atoms_reset_serial_first_value=None,
      do_not_shift_back = False):
    """
    move all the writing here later.
    """

    if do_not_shift_back and self._shift_cart is None:
      do_not_shift_back = False
    cs_to_output = self._figure_out_cs_to_output(
        do_not_shift_back=do_not_shift_back, output_cs=output_cs)

    result = StringIO()
    # outputting HELIX/SHEET records
    ss_records = ""
    ss_ann = self._get_ss_annotations_for_output()
    if ss_ann is not None:
      ss_records = ss_ann.as_pdb_str()
    if ss_records != "":
      if ss_records[-1] != "\n":
        ss_records += "\n"
      result.write(ss_records)

    #
    # Here should be NCS output somehow
    #
    if (self.link_records_in_pdb_format is not None
        and len(self.link_records_in_pdb_format)>0):
      result.write("%s\n" % self.link_records_in_pdb_format)

    hierarchy_to_output = self._figure_out_hierarchy_to_output(
        do_not_shift_back=do_not_shift_back)

    if hierarchy_to_output is not None:
      result.write(hierarchy_to_output.as_pdb_string(
          crystal_symmetry=cs_to_output,
          atoms_reset_serial_first_value=atoms_reset_serial_first_value,
          append_end=True))
    return result.getvalue()

  def extract_restraints_as_cif_blocks(self, skip_residues=None):
    restraints = iotbx.cif.model.cif()
    mon_lib_srv = self.get_mon_lib_srv()
    ph = self.get_hierarchy()
    if skip_residues is None:
      skip_residues = list(one_letter_given_three_letter.keys()) + ['HOH']
    done = []
    chem_comps = []
    for ag in ph.atom_groups():
      if ag.resname in skip_residues: continue
      if ag.resname in done: continue
      done.append(ag.resname)
      ccid = mon_lib_srv.get_comp_comp_id_direct(ag.resname.strip())
      if ccid is None:
        # printing here is highly discouraged because it makes
        # log and screen output inconsistent in refinement programs.
        # Need to rethink this idea. Accept log for def model_as_mmcif()?
        # Is it really necessary to output this?
        # if ag.resname.strip() not in ['DA', 'DC', 'DG', 'DT']:
        #   print 'writing mmCIF without restraints for %s' % ag.resname
        continue
      chem_comps.append(ccid.chem_comp)
      restraints['comp_%s' % ag.resname.strip()] = ccid.cif_object
    chem_comp_loops = []
    for cc in chem_comps:
      chem_comp_loops.append(cc.as_cif_loop())
    for key, block in restraints.items():
      for loop in block.iterloops():
        if loop is None: continue
        if '_chem_comp_plane_atom.comp_id' in loop.keys():
          # plane atom - add plane
          plane_ids = []
          comp_id = loop.get('_chem_comp_plane_atom.comp_id')[0]
          for k, item in six.iteritems(loop):
            if k=='_chem_comp_plane_atom.plane_id':
              for plane_id in item:
                if plane_id not in plane_ids: plane_ids.append(plane_id)
          plane_loop = iotbx.cif.model.loop(header=[
            '_chem_comp_plane.comp_id',
            '_chem_comp_plane.id',
            ])
          for plane_id in plane_ids:
            plane_loop.add_row([comp_id, plane_id])
          block.add_loop(plane_loop)
        if '_chem_link_bond.link_id' in loop.keys():
          # link id
          comp_id = loop.get('_chem_link_bond.link_id')[0]
          link_loop = iotbx.cif.model.loop(header=[
            '_chem_link.id',
            ])
          link_loop.add_row([comp_id])
          block.add_loop(link_loop)
      for cc in chem_comp_loops:
        cc_id = cc.get('_chem_comp.id')[0]
        if key=='comp_%s' % cc_id:
          block.add_loop(cc)
          break
    return restraints

  def _get_ss_annotations_for_output(self):
    ss_ann = None
    if self._ss_manager is not None:
      ss_ann = self._ss_manager.actual_sec_str
    elif self.get_ss_annotation() is not None:
      ss_ann = self.get_ss_annotation()
    if ss_ann is not None:
      ss_ann.remove_empty_annotations(self.get_hierarchy())
    return ss_ann

  def model_as_mmcif(self,
      cif_block_name = "default",
      output_cs = True,
      additional_blocks = None,
      align_columns = False,
      do_not_shift_back = False):
    out = StringIO()
    cif = iotbx.cif.model.cif()
    cif_block = None
    cs_to_output = self._figure_out_cs_to_output(
        do_not_shift_back=do_not_shift_back, output_cs=output_cs)
    if cs_to_output is not None:
      cif_block = cs_to_output.as_cif_block()

    hierarchy_to_output = self._figure_out_hierarchy_to_output(
        do_not_shift_back=do_not_shift_back)
    if hierarchy_to_output is not None:
      if cif_block is not None:
        cif_block.update(hierarchy_to_output.as_cif_block())
      else:
        cif_block = hierarchy_to_output.as_cif_block()

    if self.restraints_manager_available():
      ias_selection = self.get_ias_selection()
      sites_cart = self.get_sites_cart()
      # using the output hierarchy because xyz not used in struct_conn loop
      atoms = hierarchy_to_output.atoms()
      if ias_selection and ias_selection.count(True) > 0:
        sites_cart = sites_cart.select(~ias_selection)
        atoms = atoms.select(~ias_selection)
      grm_geometry = self.get_restraints_manager().geometry
      grm_geometry.pair_proxies(sites_cart)
      struct_conn_loop = grm_geometry.get_struct_conn_mmcif(hierarchy_to_output)
      cif_block.add_loop(struct_conn_loop)
      self.get_model_statistics_info()
    # outputting HELIX/SHEET records
    ss_cif_loops = []
    ss_ann = self._get_ss_annotations_for_output()
    if ss_ann is not None:
      ss_cif_loops = ss_ann.as_cif_loops()
    for loop in ss_cif_loops:
      cif_block.add_loop(loop)

    # add sequence information
    if self._sequence_validation is not None:
      cif_block.update(self._sequence_validation.sequence_as_cif_block())

    if self.model_statistics_info is not None:
      cif_block.update(self.model_statistics_info.as_cif_block())
      # adding NCS information.
      # It is not clear why we dump cartesian NCS first, and if it is absent,
      # Torsion NCS next. What about NCS constraints?
      if self.ncs_constraints_present():
        cif_block.update(self.get_ncs_groups().as_cif_block(
            cif_block=cif_block,
            hierarchy=self.get_hierarchy(),
            scattering_type=self.model_statistics_info.get_pdbx_refine_id(),
            ncs_type="NCS constraints"))
      elif self.cartesian_NCS_present():
        cif_block.update(self.get_restraints_manager().cartesian_ncs_manager.\
            as_cif_block(
                cif_block=cif_block,
                hierarchy=self.get_hierarchy(),
                scattering_type=self.model_statistics_info.get_pdbx_refine_id()))
      elif self.torsion_NCS_present():
        cif_block.update(self.get_restraints_manager().geometry.ncs_dihedral_manager.\
            as_cif_block(
                cif_block=cif_block,
                hierarchy=self.get_hierarchy(),
                scattering_type=self.model_statistics_info.get_pdbx_refine_id()))

    if additional_blocks is not None:
      for ab in additional_blocks:
        cif_block.update(ab)
    cif_block.sort(key=category_sort_function)
    cif[cif_block_name] = cif_block

    restraints = self.extract_restraints_as_cif_blocks()
    cif.update(restraints)

    if self.restraints_manager_available():
      links = grm_geometry.get_cif_link_entries(self.get_mon_lib_srv())
      cif.update(links)
    cif.show(out=out, align_columns=align_columns)
    return out.getvalue()

  def restraints_as_geo(self,
      header = "# Geometry restraints\n",
      excessive_distance_limit = 1.5,
      force=False):
    """
    get geo file as string.
    force = True actually will try to build GRM. Not advised, because if
    it is not already build, most likely a program never needed it, so
    no reason to output.
    """
    result = StringIO()
    if force:
      self.get_restraints_manager()
    self.restraints_manager.write_geo_file(
        hierarchy = self.get_hierarchy(),
        sites_cart=self.get_sites_cart(),
        site_labels=self.get_site_labels(),
        header=header,
        # Stuff for outputting ncs_groups
        excessive_distance_limit = excessive_distance_limit,
        xray_structure=self.get_xray_structure(),
        file_descriptor=result)
    return result.getvalue()

  def get_site_labels(self):
    return self.get_xray_structure().scatterers().extract_labels()

  def input_model_format_cif(self):
    return self._original_model_format == "mmcif"

  def input_model_format_pdb(self):
    return self._original_model_format == "pdb"

  def model_as_str(self, output_cs=True):
    if(  self.input_model_format_cif()):
      return self.model_as_mmcif(output_cs=output_cs)
    elif(self.input_model_format_pdb()):
      return self.model_as_pdb(output_cs=output_cs)
    else: raise RuntimeError("Model source is unknown.")

  def process_input_model(
        self,
        make_restraints    = False,
        grm_normalization  = True,
        plain_pairs_radius = 5,
        custom_nb_excl     = None,
        run_clash_guard    = False):
    # Not clear if we can handle this correctly for self._xray_structure
    # assert self.get_number_of_models() < 2
    # assert 0
    if self._processed_pdb_files_srv is None:
      self._processed_pdb_files_srv = mmtbx.utils.process_pdb_file_srv(
          crystal_symmetry          = self.crystal_symmetry(),
          pdb_interpretation_params = self._pdb_interpretation_params.pdb_interpretation,
          stop_for_unknowns         = self._stop_for_unknowns,
          log                       = self.log,
          cif_objects               = self._restraint_objects,
          cif_parameters            = self._monomer_parameters, # mmtbx.utils.cif_params scope - should be refactored to remove
          mon_lib_srv               = None,
          ener_lib                  = None,
          use_neutron_distances     = self._pdb_interpretation_params.pdb_interpretation.use_neutron_distances)
    if self._processed_pdb_file is None:
      self._processed_pdb_file, junk = self._processed_pdb_files_srv.process_pdb_files(
          pdb_inp = self._model_input,
          hierarchy = self._pdb_hierarchy,
          # because hierarchy already extracted
          # raw_records = flex.split_lines(self._pdb_hierarchy.as_pdb_string()),
          # stop_if_duplicate_labels = True,
          allow_missing_symmetry=True)
    if self.all_chain_proxies is None:
      self.all_chain_proxies = self._processed_pdb_file.all_chain_proxies
    self._atom_selection_cache = self._processed_pdb_file.all_chain_proxies.pdb_hierarchy.atom_selection_cache()
    self._pdb_hierarchy = self._processed_pdb_file.all_chain_proxies.pdb_hierarchy
    xray_structure_all = \
          self._processed_pdb_file.xray_structure(show_summary = False)
    # XXX ad hoc manipulation
    for sc in xray_structure_all.scatterers():
      lbl=sc.label.split()
      if("IAS" in lbl and sc.scattering_type=="?" and lbl[1].startswith("IS")):
        sc.scattering_type = lbl[1]
    #
    if(xray_structure_all is None):
      raise Sorry("Cannot extract xray_structure.")
    if(xray_structure_all.scatterers().size()==0):
      raise Sorry("Empty xray_structure.")
    if self.all_chain_proxies is not None:
      self.all_chain_proxies = self._processed_pdb_file.all_chain_proxies
    self._xray_structure = xray_structure_all

    self._mon_lib_srv = self._processed_pdb_files_srv.mon_lib_srv
    self._ener_lib = self._processed_pdb_files_srv.ener_lib
    self._ncs_obj = self._processed_pdb_file.ncs_obj
    self._update_has_hd()
    #
    if(make_restraints):
      self._setup_restraints_manager(
       grm_normalization  = grm_normalization,
       plain_pairs_radius = plain_pairs_radius,
       custom_nb_excl     = custom_nb_excl,
       run_clash_guard    = run_clash_guard)
    #
    self._clash_guard_msg = self._processed_pdb_file.clash_guard(
      new_sites_cart = self.get_sites_cart())
    # This must happen after process_input_model call.
    # Reason: contents of model and _model_input can get out of sync any time.
    self._model_input = None
    self._processed_pdb_file = None

  def has_hd(self):
    if self._has_hd is None:
      self._update_has_hd()
    return self._has_hd

  def _update_has_hd(self):
    sctr_keys = self.get_xray_structure().scattering_type_registry().type_count_dict()
    self._has_hd = "H" in sctr_keys or "D" in sctr_keys
    if not self._has_hd:
      self.unset_riding_h_manager()
    if self._has_hd:
      self.exchangable_hd_groups = utils.combine_hd_exchangable(
        hierarchy = self._pdb_hierarchy)

  def _update_atom_selection_cache(self):
    if self.all_chain_proxies is not None:
      self._atom_selection_cache = self.all_chain_proxies.pdb_hierarchy.atom_selection_cache()
    elif self.crystal_symmetry() is not None:
      self._atom_selection_cache = self.get_hierarchy().atom_selection_cache(
          special_position_settings=crystal.special_position_settings(
              crystal_symmetry = self.crystal_symmetry() ))
    else:
      self._atom_selection_cache = self.get_hierarchy().atom_selection_cache()

  def unset_restraints_manager(self):
    self.restraints_manager = None
    self.model_statistics_info = None
    self._processed_pdb_file = None
    self._processed_pdb_files_srv = None

  def raise_clash_guard(self):
    if self._clash_guard_msg is not None:
      raise Sorry(self._clash_guard_msg)

  def _setup_restraints_manager(
      self,
      grm_normalization = True,
      external_energy_function = None,
      plain_pairs_radius=5.0,
      custom_nb_excl=None,
      run_clash_guard = True,
      ):
    if(self.restraints_manager is not None): return
    assert self._processed_pdb_file is not None
    geometry = self._processed_pdb_file.geometry_restraints_manager(
      show_energies      = False,
      plain_pairs_radius = plain_pairs_radius,
      params_edits       = self._pdb_interpretation_params.geometry_restraints.edits,
      params_remove      = self._pdb_interpretation_params.geometry_restraints.remove,
      custom_nonbonded_exclusions  = custom_nb_excl,
      external_energy_function=external_energy_function,
      assume_hydrogens_all_missing = not self.has_hd())
    if run_clash_guard:
      self.raise_clash_guard()

    # Link treating should be rewritten. They should not be saved in
    # all_chain_proxies and they should support mmcif.
    self.link_records_in_pdb_format = link_record_output(self._processed_pdb_file.all_chain_proxies)

    self._ss_manager = self._processed_pdb_file.ss_manager

    # For test GRM pickling
    # from cctbx.regression.tst_grm_pickling import make_geo_pickle_unpickle
    # geometry = make_geo_pickle_unpickle(
    #     geometry=geometry,
    #     xrs=xray_structure,
    #     prefix=None)

    # For test GRM pickling
    # from cctbx.regression.tst_grm_pickling import make_geo_pickle_unpickle
    # geometry = make_geo_pickle_unpickle(
    #     geometry=geometry,
    #     xrs=xray_structure,
    #     prefix=None)
    if hasattr(self._pdb_interpretation_params, "reference_model"):
      add_reference_dihedral_restraints_if_requested(
          self,
          geometry=geometry,
          params=self._pdb_interpretation_params.reference_model,
          selection=None,
          log=self.log)

    ############################################################################
    # Switch in external alternative geometry manager. Options include:
    #  1. Amber force field
    #  2. Schrodinger force field
    ############################################################################
    params = self._pdb_interpretation_params
    if hasattr(params, 'amber') and params.amber.use_amber:
      from amber_adaptbx.manager import digester
      geometry = digester(geometry, params, log=self.log)
    elif hasattr(params, "schrodinger") and params.schrodinger.use_schrodinger:
      from phenix_schrodinger import schrodinger_manager
      geometry = schrodinger_manager(self._pdb_hierarchy,
                                     params,
                                     cleanup=True,
                                     grm=geometry)

    restraints_manager = mmtbx.restraints.manager(
      geometry      = geometry,
      normalization = grm_normalization)
    # Torsion restraints from reference model
    if(self._xray_structure is not None):
      restraints_manager.crystal_symmetry = self._xray_structure.crystal_symmetry()
    self.restraints_manager = restraints_manager
    #
    # Here we do all what is necessary when GRM and all related become available
    #

  def set_reference_coordinate_restraints(self,
      ref_model,
      selection="all",
      exclude_outliers=True,
      sigma=0.2,
      limit=1.0,
      top_out=False):
    rm = self.get_restraints_manager().geometry
    rm.remove_reference_coordinate_restraints_in_place()

    exclude_selection_ref = flex.bool(ref_model.get_number_of_atoms(), False)
    exclude_selection_self = flex.bool(self.get_number_of_atoms(), False)
    for sel, m in [(exclude_selection_ref, ref_model), (exclude_selection_self, self)]:
      if m.has_hd():
        sel |= m.get_hd_selection()
      sel |= m.selection('water')

    reference_hierarchy = ref_model.get_hierarchy().select(~exclude_selection_ref)
    self_hierarchy = self.get_hierarchy().select(~exclude_selection_self)
    # sanity check
    for i, a in enumerate(self_hierarchy.atoms()):
      ref_a = reference_hierarchy.atoms()[i]
      # print "Sanity check: '%s' == '%s'" % (a.id_str(), ref_a.id_str())
      if a.id_str() != ref_a.id_str():
        raise Sorry("Something went wrong in setting reference coordinate restraints." + \
            "Please refer this case to bugs@phenix-online.org"+ \
            "'%s' != '%s'" % (a.id_str(), ref_a.id_str()))

    rm.add_reference_coordinate_restraints_in_place(
        pdb_hierarchy=reference_hierarchy,
        selection=(~exclude_selection_self).iselection(),
        exclude_outliers=exclude_outliers,
        sigma=sigma,
        limit=limit,
        top_out=top_out,
        n_atoms_in_target_model=self.get_number_of_atoms())

  def set_reference_torsion_restraints(self, ref_model, params=None):
    geometry = self.get_restraints_manager().geometry
    geometry.remove_reference_dihedral_manager()

    if params is None:
      params = iotbx.phil.parse(reference_model_str).extract()
      params.reference_model.enabled=True
    ter_indices = self._ter_indices
    if ter_indices is not None:
      check_for_internal_chain_ter_records(
        pdb_hierarchy=self.get_hierarchy(),
        ter_indices=ter_indices)
    rm = reference_model(
      self,
      reference_file_list=None,
      reference_hierarchy_list=[ref_model.get_hierarchy()],
      params=params.reference_model,
      selection=None,
      log=self.log)
    rm.show_reference_summary(log=self.log)
    geometry.adopt_reference_dihedral_manager(rm)

  #
  # =======================================================================
  # NCS-related features.
  # =======================================================================
  #

  def setup_torsion_ncs_restraints(self,
      fmodel,
      ncs_torsion_params,
      sites_individual,
      log):
    check_for_internal_chain_ter_records(
        pdb_hierarchy = self.get_hierarchy(),
        ter_indices   = self._ter_indices)
    ncs_obj = self.get_ncs_obj()
    if ncs_obj is None: return
    geometry = self.get_restraints_manager().geometry
    if ncs_obj.number_of_ncs_groups > 0:
      print("\n", file=log)
      geometry.ncs_dihedral_manager = torsion_ncs(
          model              = self,
          fmodel             = fmodel,
          params             = ncs_torsion_params,
          selection          = sites_individual,
          log                = log)
      if geometry.ncs_dihedral_manager.get_n_proxies() == 0:
        geometry.ncs_dihedral_manager = None
    geometry.sync_reference_dihedral_with_ncs(log=log)

  def torsion_NCS_present(self):
    rm = self.get_restraints_manager()
    if rm is None:
      return False
    if rm.geometry.ncs_dihedral_manager is None:
      return False
    return True

  def torsion_NCS_as_pdb(self):
    result = StringIO()
    if self.torsion_NCS_present():
      self.get_restraints_manager().geometry.ncs_dihedral_manager.as_pdb(
          out=result)
    return result.getvalue()

  def torsion_ncs_restraints_update(self, log=None):
    if self.get_restraints_manager() is not None:
      self.get_restraints_manager().geometry.update_dihedral_ncs_restraints(
          model=self,
          log=log)

  def ncs_constraints_present(self):
    g = self.get_ncs_groups()
    return g is not None and len(g)>0

  def search_for_ncs(self, params=None, log=null_out()):
    self._ncs_obj = iotbx.ncs.input(
        hierarchy=self.get_hierarchy(),
        params=params,
        log=log)
    if self._ncs_obj is not None:
      self._ncs_groups = self.get_ncs_obj().get_ncs_restraints_group_list()
    self._update_master_sel()


  def setup_ncs_constraints_groups(self, filter_groups=False):
    """
    This will be used directly (via get_ncs_groups) in
    mmtbx/refinement/minimization.py, mmtbx/refinement/adp_refinement.py

    supposedly NCS constraints
    """
    if self.get_ncs_obj() is not None:
      self._ncs_groups = self.get_ncs_obj().get_ncs_restraints_group_list()
      if filter_groups:
        # set up new groups for refinements
        self._ncs_groups = self._ncs_groups.filter_ncs_restraints_group_list(
            whole_h=self.get_hierarchy(),
            ncs_obj=self.get_ncs_obj())
      self.get_ncs_obj().set_ncs_restraints_group_list(self._ncs_groups)
    self._update_master_sel()

  def _update_master_sel(self):
    if self._ncs_groups is not None and len(self._ncs_groups) > 0:
      # determine master selections
      self._master_sel = flex.bool(self.get_number_of_atoms(), True)
      for ncs_gr in self._ncs_groups:
        for copy in ncs_gr.copies:
          self._master_sel.set_selected(copy.iselection, False)
    else:
      self._master_sel = flex.bool(self.get_number_of_atoms(), True)


  def get_master_hierarchy(self):
    assert self.get_hierarchy().models_size() == 1
    if self._master_sel.size() > 0:
      return self.get_hierarchy().select(self._master_sel)
    else:
      return self.get_hierarchy()

  def get_master_selection(self):
    return self._master_sel

  def get_ncs_obj(self):
    return self._ncs_obj

  def get_ncs_groups(self):
    """
    This returns ncs_restraints_group_list object
    """
    return self._ncs_groups

  def update_ncs_operators(self):
    ncs_groups = self.get_ncs_groups()
    if(ncs_groups is not None):
      ncs_groups.recalculate_ncs_transforms(
        asu_site_cart = self.get_sites_cart())

  def unset_ncs_constraints_groups(self):
    self._ncs_groups=None
    self._ncs_obj=None
    # shouldn't be None, probably flex.bool(self.get_number_of_atoms(), True)
    self._master_sel=None

  def setup_cartesian_ncs_groups(self, ncs_params=None, log=null_out()):
    import mmtbx.ncs.cartesian_restraints
    cartesian_ncs = mmtbx.ncs.cartesian_restraints.cartesian_ncs_manager(
        model=self,
        ncs_params=ncs_params)
    rm = self.get_restraints_manager()
    if cartesian_ncs.get_n_groups() > 0:
      assert rm is not None
      rm.cartesian_ncs_manager = cartesian_ncs
    else:
      print("No NCS restraint groups specified.", file=self.log)
      print(file=self.log)

  def get_vdw_radii(self, vdw_radius_default = 1.0):
    """
    Return van-der-Waals radii for known atom names.
    """
    m = self.get_mon_lib_srv()
    e = self.get_ener_lib()
    e_lib_atom_keys = e.lib_atom.keys()
    result = {}
    for k0,v0 in six.iteritems( m.comp_comp_id_dict):
      for k1,v1 in six.iteritems(v0.atom_dict()):
        if(v1.type_energy in e_lib_atom_keys):
          vdw_radius = e.lib_atom[v1.type_energy].vdw_radius
          if(vdw_radius is None):
            vdw_radius = vdw_radius_default
            if(self.log is not None):
              msg = "WARNING: vdw radius undefined for: (%s %s); setting to: %s"
              print(msg%(k1, v1.type_energy, vdw_radius_default), file=self.log)
          result[k1] = vdw_radius
    return result

  def get_n_excessive_site_distances_cartesian_ncs(self, excessive_distance_limit=1.5):
    result = 0
    if (self.get_restraints_manager() is not None and
        self.get_restraints_manager().cartesian_ncs_manager is not None):
      result = self.get_restraints_manager().cartesian_ncs_manager.\
          get_n_excessive_sites_distances()
      if result is None: # nobody run show_sites yet
        result = self.get_restraints_manager().cartesian_ncs_manager.\
            show_sites_distances_to_average(
                sites_cart=self.get_sites_cart(),
                site_labels=self.get_site_labels(),
                excessive_distance_limit=excessive_distance_limit,
                out=null_out())
    return result

  def raise_excessive_site_distances_cartesian_ncs(self, excessive_distance_limit=1.5):
    n_excessive = self.get_n_excessive_site_distances_cartesian_ncs(
        excessive_distance_limit=excessive_distance_limit)
    if n_excessive > 0:
      raise Sorry("Excessive distances to NCS averages:\n"
        + "  Please inspect the resulting .geo file\n"
        + "  for a full listing of the distances to the NCS averages.\n"
        + '  Look for the word "EXCESSIVE".\n'
        + "  The current limit is defined by parameter:\n"
        + "    refinement.ncs.excessive_distance_limit\n"
        + "  The number of distances exceeding this limit is: %d\n"
            % n_excessive
        + "  Please correct your model or redefine the limit.\n"
        + "  To disable this message completely define:\n"
        + "    refinement.ncs.excessive_distance_limit=99999")

  def cartesian_NCS_as_pdb(self):
    result = StringIO()
    if (self.restraints_manager is not None and
        self.restraints_manager.cartesian_ncs_manager is not None):
      self.restraints_manager.cartesian_ncs_manager.as_pdb(
          sites_cart=self.get_sites_cart(),
          out=result)
    return result.getvalue()

  def cartesian_NCS_present(self):
    c_ncs = self.get_cartesian_NCS_manager()
    if c_ncs is not None:
      return c_ncs.get_n_groups() > 0
    return False

  def get_cartesian_NCS_manager(self):
    if self.get_restraints_manager() is not None:
      return self.get_restraints_manager().cartesian_ncs_manager
    return None

  def scattering_dictionary(self):
    if(self._xray_structure is None): return None
    return self._xray_structure.scattering_type_registry().as_type_gaussian_dict()

  def setup_scattering_dictionaries(self,
      scattering_table,
      d_min=None,
      log = None,
      set_inelastic_form_factors=None,
      iff_wavelength=None):
    self.get_xray_structure()
    self.scattering_dict_info = group_args(
        scattering_table=scattering_table,
        d_min = d_min,
        set_inelastic_form_factors=set_inelastic_form_factors,
        iff_wavelength=iff_wavelength)
    if(log is not None):
      str_utils.make_header("Scattering factors", out = log)
    known_scattering_tables = [
      "n_gaussian", "wk1995", "it1992", "electron", "neutron"]
    if(not (scattering_table in known_scattering_tables)):
      raise Sorry("Unknown scattering_table: %s\n%s"%
        (str_utils.show_string(scattering_table),
        "Possible choices are: %s"%" ".join(known_scattering_tables)))
    if(scattering_table in ["n_gaussian", "wk1995", "it1992", "electron"]):
      self._xray_structure.scattering_type_registry(
        table = scattering_table,
        d_min = d_min,
        types_without_a_scattering_contribution=["?"])
      self._xray_structure.scattering_type_registry(
        custom_dict = ias.ias_scattering_dict)
      self.xray_scattering_dict = \
        self._xray_structure.scattering_type_registry().as_type_gaussian_dict()
      if(log is not None):
        print_statistics.make_sub_header("X-ray scattering dictionary",out=log)
        self._xray_structure.scattering_type_registry().show(out = log)
    if(scattering_table == "neutron"):
      try :
        self.neutron_scattering_dict = \
          self._xray_structure.switch_to_neutron_scattering_dictionary()
      except ValueError as e :
        raise Sorry("Error setting up neutron scattering dictionary: %s"%str(e))
      if(log is not None):
        print_statistics.make_sub_header(
          "Neutron scattering dictionary", out = log)
        self._xray_structure.scattering_type_registry().show(out = log)
      self._xray_structure.scattering_type_registry_params.table = "neutron"
    if self.all_chain_proxies is not None:
      scattering_type_registry = self.all_chain_proxies.scattering_type_registry
      if(scattering_type_registry.n_unknown_type_symbols() > 0):
        scattering_type_registry.report(
          pdb_atoms = self.get_hierarchy().atoms(),
          log = log,
          prefix = "",
          max_lines = None)
        raise Sorry("Unknown scattering type symbols.\n"
          "  Possible ways of resolving this error:\n"
          "    - Edit columns 77-78 in the PDB file to define"
            " the scattering type.\n"
          "    - Provide custom monomer definitions for the affected residues.")
      if(log is not None):
        print(file=log)
    if set_inelastic_form_factors is not None and iff_wavelength is not None:
      self._xray_structure.set_inelastic_form_factors(
          photon=iff_wavelength,
          table=set_inelastic_form_factors)
    return self.xray_scattering_dict, self.neutron_scattering_dict

  def get_searched_tls_selections(self, nproc, log):
    if "searched_tls_selections" not in self.__dict__.keys():
      tls_params = find_tls_groups.master_phil.fetch().extract()
      tls_params.nproc = nproc
      self.searched_tls_selections = find_tls_groups.find_tls(
        params=tls_params,
        pdb_hierarchy=self._pdb_hierarchy,
        xray_structure=deepcopy(self._xray_structure),
        return_as_list=True,
        ignore_pdb_header_groups=True,
        out=log)
    return self.searched_tls_selections

  def determine_tls_groups(self, selection_strings, generate_tlsos):
    self.tls_groups = tls_tools.tls_groups(selection_strings = selection_strings)
    if generate_tlsos is not None:
      # generate_tlsos here are actually [isel, isel, ..., isel]
      tlsos = tls_tools.generate_tlsos(
        selections     = generate_tlsos,
        xray_structure = self._xray_structure,
        value          = 0.0)
      self.tls_groups.tlsos = tlsos
      self.tls_groups.iselections = generate_tlsos

  def tls_groups_as_pdb(self):
    out = StringIO()
    if self.tls_groups is not None:
      tls_tools.remark_3_tls(
          tlsos             = self.tls_groups.tlsos,
          selection_strings = self.tls_groups.selection_strings,
          out               = out)
    return out.getvalue()

  def tls_groups_as_cif_block(self, cif_block=None):
    if self.tls_groups is not None:
      cif_block = self.tls_groups.as_cif_block(
        hierarchy=self.get_hierarchy(),
        cif_block=cif_block,
        scattering_type=self.model_statistics_info.get_pdbx_refine_id())
    return cif_block

  def get_model_input(self):
    return self._model_input

  def get_riding_h_manager(self, idealize=True, force=False):
    """
    Force=True: Force creating manager if hydrogens are available
    """
    if self.riding_h_manager is None and force:
      self.setup_riding_h_manager(idealize=True)
    return self.riding_h_manager

  def unset_riding_h_manager(self):
    self.riding_h_manager = None

  def setup_riding_h_manager(self, idealize=True):
    assert self.riding_h_manager is None
    if not self.has_hd(): return
    if(self.restraints_manager is None): return
    self.riding_h_manager = riding.manager(
      pdb_hierarchy       = self.get_hierarchy(),
      geometry_restraints = self.get_restraints_manager().geometry)
    if(idealize):
      self.idealize_h_riding()

  def idealize_h_riding(self):
    if self.riding_h_manager is None:
      self.setup_riding_h_manager(idealize=True)
    else:
      sites_cart = self.get_sites_cart()
      if self.refinement_flags:
        flags = self.refinement_flags.sites_individual
      else:
        flags = None
      self.riding_h_manager.idealize_riding_h_positions(
        sites_cart=sites_cart,
        selection_bool = flags)
      self.set_sites_cart(sites_cart)

  def get_hierarchy(self):
    """
    Accessor for the underlying PDB hierarchy, incorporating optional update of
    properties from the xray_structure attribute.
    """
    return self._pdb_hierarchy

  def set_sites_cart_from_hierarchy(self, multiply_ncs=False):
    if (multiply_ncs and self.ncs_constraints_present()):
      self._update_master_sel()
      new_coords = apply_transforms(
          ncs_coordinates=self.get_master_hierarchy().atoms().extract_xyz(),
          ncs_restraints_group_list=self.get_ncs_groups(),
          total_asu_length=self.get_number_of_atoms(),
          extended_ncs_selection=self._master_sel,
          round_coordinates = False,
          center_of_coordinates = None)
      self.get_hierarchy().atoms().set_xyz(new_coords)
    self.get_xray_structure().set_sites_cart(self._pdb_hierarchy.atoms().extract_xyz())
    self.get_hierarchy().atoms().reset_i_seq()
    self.model_statistics_info = None

  def normalize_adjacent_adp(self, threshold = 20):
    if(self.restraints_manager is None): return
    bond_proxies_simple, asu = \
      self.restraints_manager.geometry.get_all_bond_proxies(
        sites_cart = self.get_sites_cart())
    b_isos = self.get_b_iso()
    for proxy in bond_proxies_simple:
      i_seq, j_seq = proxy.i_seqs
      bi = b_isos[i_seq]
      bj = b_isos[j_seq]
      if(abs(bi-bj)>threshold):
        if(bi>bj): bi=bj+threshold
        else:      bj=bi+threshold
        b_isos[i_seq] = bi
        b_isos[j_seq] = bj
    self.set_b_iso(values = b_isos)

  def xh_connectivity_table(self):
    result = None
    if(self.restraints_manager is not None):
      if self.has_hd():
        xray_structure = self._xray_structure
        ias_selection = self.get_ias_selection()
        if ias_selection and ias_selection.count(True) > 0:
          xray_structure = self._xray_structure.select(~ias_selection)
        result = xh_connectivity_table(
          geometry       = self.restraints_manager,
          xray_structure = xray_structure).table
    return result

  def xh_connectivity_table2(self):
    result = None
    if(self.restraints_manager is not None):
      if self.has_hd():
        xray_structure = self._xray_structure
        ias_selection = self.get_ias_selection()
        if ias_selection and ias_selection.count(True) > 0:
          xray_structure = self._xray_structure.select(~ias_selection)
        result = xh_connectivity_table2(
          geometry       = self.restraints_manager,
          xray_structure = xray_structure).table
    return result

  def extend_xh_bonds(self, value=1.1):
    if(self.restraints_manager is None): return
    if not self.has_hd(): return
    assert self.original_xh_lengths is None
    h_i_seqs = []
    xhct = self.xh_connectivity_table()
    if(xhct is None): return
    self.original_xh_lengths = flex.double()
    for xhcti in xhct:
      h_i_seqs.append(xhcti[1])
    for bp in self.restraints_manager.geometry.bond_params_table:
      for i, k in enumerate(bp.keys()):
        if(k in h_i_seqs):
          # FIXME, if bp is a dictionary this will prpobably break py2/3 funcionality
          self.original_xh_lengths.append(list(bp.values())[i].distance_ideal)
          list(bp.values())[i].distance_ideal = value

  def restore_xh_bonds(self):
    if(self.restraints_manager is None): return
    if not self.has_hd(): return
    assert self.original_xh_lengths is not None
    xhct = self.xh_connectivity_table()
    if(xhct is None): return
    h_i_seqs = []
    for xhcti in xhct:
      h_i_seqs.append(xhcti[1])
    counter = 0
    for bp in self.restraints_manager.geometry.bond_params_table:
      for i, k in enumerate(bp.keys()):
        if(k in h_i_seqs):
          # FIXME: python2/3 breakage if bp is a dict
          list(bp.values())[i].distance_ideal = self.original_xh_lengths[counter]
          counter += 1
    self.original_xh_lengths = None
    self.idealize_h_minimization(show=False)

  def isolated_atoms_selection(self):
    if(self.restraints_manager is None):
      raise Sorry("Geometry restraints manager must be defined.")
    selection = flex.bool(self._xray_structure.scatterers().size(), True)
    bond_proxies_simple, asu = self.restraints_manager.geometry.\
        get_all_bond_proxies(sites_cart=self._xray_structure.sites_cart())
    for proxy in bond_proxies_simple:
      i_seq, j_seq = proxy.i_seqs
      selection[i_seq] = False
      selection[j_seq] = False
    return selection

  def reset_adp_for_hydrogens(self, scale=1.2):
    """
    Set the isotropic B-factor for all hydrogens to those of the associated
    heavy atoms (using the total isotropic equivalent) times a scale factor of
    1.2.
    """
    if(self.restraints_manager is None): return
    hd_sel = self.get_hd_selection()
    if(hd_sel.count(True) > 0):
      assert self._xray_structure is not None
      xh_conn_table = self.xh_connectivity_table()
      bfi = self._xray_structure.extract_u_iso_or_u_equiv()
      for t in self.xh_connectivity_table():
        i_x, i_h = t[0], t[1]
        bfi[i_h] = adptbx.u_as_b(bfi[i_x])*scale
      self.set_b_iso(values = bfi, selection = hd_sel)

  def reset_occupancy_for_hydrogens_simple(self):
    """
    Set occupancy of H to be the same as the parent.
    """
    if(self.restraints_manager is None): return
    hd_sel = self.get_hd_selection()
    if(hd_sel.count(True) > 0):
      assert self._xray_structure is not None
      xh_conn_table = self.xh_connectivity_table()
      occ = self.get_occ()
      for t in self.xh_connectivity_table():
        i_x, i_h = t[0], t[1]
        occ[i_h] = occ[i_x]
      self.set_occupancies(values = occ)

  def reset_occupancies_for_hydrogens(self):
    """
    Set hydrogen occupancies to those of the associated heavy atoms.
    """
    if(self.restraints_manager is None): return
    occupancy_refinement_selections_1d = flex.size_t()
    if(self.refinement_flags.s_occupancies is not None):
      for occsel in self.refinement_flags.s_occupancies:
        for occsel_ in occsel:
          occupancy_refinement_selections_1d.extend(occsel_)
    hd_sel = self.get_hd_selection()
    scatterers = self._xray_structure.scatterers()
    if(hd_sel.count(True) > 0):
      assert self._xray_structure is not None
      xh_conn_table = self.xh_connectivity_table()
      qi = self.get_occ()
      ct = self.xh_connectivity_table2()
      for t_ in ct.values():
        i_x, i_h = t_[0][0],t_[0][1]
        assert scatterers[i_h].element_symbol() in ["H", "D"]
        if(scatterers[i_x].element_symbol() == "N" and
           i_h in occupancy_refinement_selections_1d):
          occ = flex.double()
          for t in t_:
            if(len(t) != 5):
              for i in t:
                if(i != i_h):
                  occ.append(qi[i])
              qi[i_h] = flex.min(occ)
            else:
              qi[i_h] = qi[i_x]
        else:
          qi[i_h] = qi[i_x]
      if(self.refinement_flags.s_occupancies is not None):
        for rf1 in self.refinement_flags.s_occupancies:
          o=None
          for rf2 in rf1:
            for rf_ in rf2:
              if(not hd_sel[rf_]):
                o = qi[rf_]
            for rf_ in rf2:
              if(o is not None):
                qi[rf_] = o
      self.set_occupancies(values = qi, selection = hd_sel)

  def reset_coordinates_for_exchangable_hd(self):
    if(len(self.exchangable_hd_groups) > 0):
      occ = self.get_occ()
      sites_cart = self.get_sites_cart()
      for g in self.exchangable_hd_groups:
        i, j = g[0][0], g[1][0]
        if(occ[i] > occ[j]): sites_cart[j] = sites_cart[i]
        else:                sites_cart[i] = sites_cart[j]
      self.set_sites_cart(sites_cart = sites_cart)

  def rotatable_hd_selection(self, iselection=True, use_shortcut=True):
    rmh_sel = mmtbx.hydrogens.rotatable(
      pdb_hierarchy      = self.get_hierarchy(),
      mon_lib_srv        = self.get_mon_lib_srv(),
      restraints_manager = self.get_restraints_manager(),
      log                = self.log,
      use_shortcut       = use_shortcut)
    sel_i = []
    for s in rmh_sel: sel_i.extend(s[1])
    result = flex.size_t(sel_i)
    if(iselection): return result
    else:           return flex.bool(self.size(), result)

  def h_counts(self):
    occupancies = self._xray_structure.scatterers().extract_occupancies()
    occ_sum = flex.sum(occupancies)
    hd_selection = self.get_hd_selection()
    h_occ_sum = flex.sum(occupancies.select(hd_selection))
    sel_rot = self.rotatable_hd_selection()
    hrot_occ_sum = flex.sum(occupancies.select(sel_rot))
    return group_args(
      h_count             = hd_selection.count(True),
      h_occ_sum           = h_occ_sum,
      h_fraction_of_total = h_occ_sum/occ_sum*100.,
      hrot_count             = sel_rot.size(),
      hrot_occ_sum           = hrot_occ_sum,
      hrot_fraction_of_total = hrot_occ_sum/occ_sum*100.)

  def show_h_counts(self, prefix=""):
    hc = self.h_counts()
    print("%sTotal:"%prefix, file=self.log)
    print("%s  count: %d"%(prefix, hc.h_count), file=self.log)
    print("%s  occupancy sum: %6.2f (%s of total atoms %6.2f)"%(
      prefix, hc.h_occ_sum, "%", hc.h_fraction_of_total), file=self.log)
    print("%sRotatable:"%prefix, file=self.log)
    print("%s  count: %d"%(prefix, hc.hrot_count), file=self.log)
    print("%s  occupancy sum: %6.2f (%s of total atoms %6.2f)"%(
      prefix, hc.hrot_occ_sum, "%", hc.hrot_fraction_of_total), file=self.log)

  def scattering_types_counts_and_occupancy_sums(self, prefix=""):
    out = StringIO()
    st_counts_and_occupancy_sums = \
        self.get_xray_structure().scattering_types_counts_and_occupancy_sums()
    atoms_occupancy_sum = \
        flex.sum(self.get_xray_structure().scatterers().extract_occupancies())
    fmt = "   %5s               %10d        %8.2f"
    print(prefix+"MODEL CONTENT.", file=out)
    print(prefix+" ELEMENT        ATOM RECORD COUNT   OCCUPANCY SUM", file=out)
    for item in st_counts_and_occupancy_sums:
      print(prefix+fmt % (item.scattering_type, item.count,
        item.occupancy_sum), file=out)
    print(prefix+fmt%("TOTAL",self.get_number_of_atoms(),atoms_occupancy_sum), file=out)
    return out.getvalue()

  def neutralize_scatterers(self):
    neutralized = False
    xrs = self.get_xray_structure()
    scatterers = xrs.scatterers()
    for scatterer in scatterers:
      neutralized_scatterer = filter(lambda x: x.isalpha(), scatterer.scattering_type)
      if (neutralized_scatterer != scatterer.scattering_type):
        neutralized = True
        scatterer.scattering_type = neutralized_scatterer
    if neutralized:
      self.set_xray_structure(xray_structure = xrs)
      self.unset_restraints_manager()

  def set_hydrogen_bond_length(self,
                               use_neutron_distances = True,
                               show                  = False,
                               log                   = None):
    """
    Set X-H bond lengths to either neutron or Xray target values.
    Only elongates or shortens, no other idealization is performed.
    """
    if log is None:
      log = self.log
    pi_scope = self.get_current_pdb_interpretation_params()
    # check if current pi_params is consistent with requested X-H length mode
    if (pi_scope.pdb_interpretation.use_neutron_distances
         is not use_neutron_distances):
      pi_scope.pdb_interpretation.use_neutron_distances = use_neutron_distances
      # this will take care of resetting everything (grm, processed pdb)
      self.set_pdb_interpretation_params(params = pi_scope)
      self.process_input_model(make_restraints=True)
    geometry = self.get_restraints_manager().geometry
    hierarchy = self.get_hierarchy()
    atoms = hierarchy.atoms()
    sites_cart = self.get_sites_cart()
    bond_proxies_simple, asu = \
      geometry.get_all_bond_proxies(sites_cart = sites_cart)
    hd_selection = self.get_hd_selection()
    if show:
      if use_neutron_distances: mode = 'neutron'
      else:                     mode = 'Xray'
      print_statistics.make_sub_header("Changing X-H distances to %s" % mode,
        out=log)
      string = ['atom', 'distance initial', 'modified distance', 'distance moved']
      string_form = "{:^15}|{:^20}|{:^20}|{:^20}"
      print('\n' + string_form.format(*string), file=log)
      print('-'*99, file=log)
    n_modified = 0
    for bproxy in bond_proxies_simple:
      i_seq, j_seq = bproxy.i_seqs
      is_i_hd = hd_selection[i_seq]
      is_j_hd = hd_selection[j_seq]
      if(not is_i_hd and not is_j_hd):
        continue
      elif(is_i_hd and is_j_hd):
        continue
      else:
        if  (is_i_hd): ih, ix = i_seq, j_seq
        elif(is_j_hd): ih, ix = j_seq, i_seq
      H = atoms[ih]
      X = atoms[ix]
      distance_initial = H.distance(X)
      difference = round(bproxy.distance_ideal - distance_initial, 2)
      # only modify coordinates if different from ideal by eps
      eps = 1.e-3
      if(abs(distance_initial-bproxy.distance_ideal) < eps): continue
      rH = matrix.col(sites_cart[ih])
      rX = matrix.col(sites_cart[ix])
      uHX = (rH - rX).normalize()
      rH_new = rX + bproxy.distance_ideal * uHX
      H.set_xyz(rH_new)
      n_modified += 1
      #
      if show:
        line = [H.id_str().split('"')[1], round(distance_initial,2),
          H.distance(X), difference]
        line_form = "{:^15}|{:^20}|{:^20}|{:^20}"
        print(line_form.format(*line), file=log)
    #
    self.set_sites_cart_from_hierarchy()
    #
    if show:
      print("\n" + "Number of X-H bonds modified: %s"  % n_modified + "\n",
        file=log)

  def idealize_h_minimization(self, correct_special_position_tolerance=1.0,
                   selection=None, show=True, nuclear=False):
    """
    Perform geometry regularization on hydrogen atoms only.
    """
    if(self.restraints_manager is None): return
    if self.has_hd():
      hd_selection = self.get_hd_selection()
      if(selection is not None): hd_selection = selection
      if(hd_selection.count(True)==0): return
      not_hd_selection = ~hd_selection
      sol_hd = self.solvent_selection().set_selected(~hd_selection, False)
      mac_hd = hd_selection.deep_copy().set_selected(self.solvent_selection(), False)
      ias_selection = self.get_ias_selection()
      if (ias_selection is not None):
        not_hd_selection.set_selected(ias_selection, False)
      sites_cart_mac_before = \
        self._xray_structure.sites_cart().select(not_hd_selection)
      xhd = flex.double()
      if(hd_selection.count(True)==0): return
      for t in self.xh_connectivity_table():
        if(hd_selection[t[1]]):
          xhd.append(abs(t[-1]-t[-2]))
      if(show):
        print("X-H deviation from ideal before regularization (bond): mean=%6.3f max=%6.3f"%\
        (flex.mean(xhd), flex.max(xhd)), file=self.log)
      for sel_pair in [(mac_hd, False), (sol_hd, True)]*2:
        if(sel_pair[0].count(True) > 0):
          sel = sel_pair[0]
          if(ias_selection is not None and ias_selection.count(True) > 0):
            sel = sel.select(~ias_selection)
          minimized = geometry_minimization.run2(
              restraints_manager = self.get_restraints_manager(),
              pdb_hierarchy = self.get_hierarchy(),
              correct_special_position_tolerance = correct_special_position_tolerance,
              riding_h_manager          = None, # didn't go in original implementation
              ncs_restraints_group_list = [], # didn't go in original implementation
              max_number_of_iterations  = 500,
              number_of_macro_cycles    = 5,
              selection                 = sel,
              bond                      = True,
              nonbonded                 = sel_pair[1],
              angle                     = True,
              dihedral                  = True,
              chirality                 = True,
              planarity                 = True,
              parallelity               = True,
              log                       = StringIO(),
              mon_lib_srv               = self.get_mon_lib_srv())
          self.set_sites_cart_from_hierarchy()
      sites_cart_mac_after = \
        self._xray_structure.sites_cart().select(not_hd_selection)
      assert approx_equal(flex.max(sites_cart_mac_before.as_double() -
        sites_cart_mac_after.as_double()), 0)
      xhd = flex.double()
      for t in self.xh_connectivity_table():
        if(hd_selection[t[1]]):
          xhd.append(abs(t[-1]-t[-2]))
      if(show):
        print("X-H deviation from ideal after  regularization (bond): mean=%6.3f max=%6.3f"%\
        (flex.mean(xhd), flex.max(xhd)), file=self.log)

  def extract_water_residue_groups(self):
    result = []
    solvent_sel = self.solvent_selection()
    get_class = iotbx.pdb.common_residue_names_get_class
    for m in self._pdb_hierarchy.models():
      for chain in m.chains():
        for rg in chain.residue_groups():
          first_water = None
          first_other = None
          for ag in rg.atom_groups():
            residue_id = "%3s%4s%1s" % (ag.resname, rg.resseq, rg.icode)
            if (get_class(name=ag.resname) == "common_water"):
              for atom in ag.atoms():
                i_seq = atom.i_seq
                assert solvent_sel[i_seq]
                if (first_water is None):
                  first_water = atom
            else:
              for atom in ag.atoms():
                assert not solvent_sel[atom.i_seq]
                if (first_other is None):
                  first_other = atom
          if (first_water is not None):
            if (first_other is not None):
              raise RuntimeError(
                "residue_group with mix of water and non-water:\n"
                + "  %s\n" % first_water.quote()
                + "  %s" % first_other.quote())
            result.append(rg)
            for r in result:
              elements = r.atoms().extract_element()
              o_found = 0
              for e in elements:
                if(e.strip().upper() == 'O'): o_found += 1
              if(o_found == 0):
                print(file=self.log)
                for a in r.atoms():
                  print(a.format_atom_record(), file=self.log)
                raise Sorry(
                  "The above waters in input PDB file do not have O atom.")
    return result

  def renumber_water(self):
    for i,rg in enumerate(self.extract_water_residue_groups()):
      rg.resseq = iotbx.pdb.resseq_encode(value=i+1)
      rg.icode = " "
    self.get_hierarchy().atoms().reset_i_seq()
    self._sync_xrs_labels()

  def add_hydrogens(self, correct_special_position_tolerance,
        element = "H", neutron = False, occupancy=0.01):
    result = []
    xs = self.get_xray_structure()
    if(neutron): element = "D"
    frac = xs.unit_cell().fractionalize
    sites_cart = xs.sites_cart()
    u_isos = xs.extract_u_iso_or_u_equiv()
    next_to_i_seqs = []
    last_insert_i_seq = [sites_cart.size()]
    def insert_atoms(atom, atom_names, element):
      i_seq = atom.i_seq
      assert i_seq < last_insert_i_seq[0]
      last_insert_i_seq[0] = i_seq
      xyz = sites_cart[i_seq]
      sign = True
      for i,atom_name in enumerate(atom_names):
        h = atom.detached_copy()
        h.name = atom_name
        if(sign):
          h.xyz = [a+b for a,b in zip(xyz, (1,0.001,0))]
          sign = False
        else:
          h.xyz = [a+b for a,b in zip(xyz, (-1,0,0))]
          sign = True
        h.sigxyz = (0,0,0)
        h.occ = occupancy
        h.sigocc = 0
        h.b = adptbx.u_as_b(u_isos[i_seq])
        h.sigb = 0
        h.uij = (-1,-1,-1,-1,-1,-1)
        if (iotbx.pdb.hierarchy.atom.has_siguij()):
          h.siguij = (-1,-1,-1,-1,-1,-1)
        h.element = "%2s" % element.strip()
        ag.append_atom(atom=h)
        scatterer = xray.scatterer(
          label           = h.id_str(),
          scattering_type = h.element.strip(),
          site            = frac(h.xyz),
          u               = adptbx.b_as_u(h.b),
          occupancy       = h.occ)
        xs.add_scatterer(
          scatterer = scatterer,
          insert_at_index = i_seq+i+1)
        next_to_i_seqs.append(i_seq) # not i_seq+i because refinement_flags.add
                                     # sorts next_to_i_seqs internally :-(
    water_rgs = self.extract_water_residue_groups()
    water_rgs.reverse()
    for rg in water_rgs:
      if (rg.atom_groups_size() != 1):
        raise Sorry("Not implemented: cannot add hydrogens to water "+
                    "molecules with alternate conformations")
      ag = rg.only_atom_group()
      atoms = ag.atoms()
      # do not add H or D to O at or close to special position
      skip = False
      sps = self._xray_structure.special_position_settings(
        min_distance_sym_equiv=3.0)
      for atom in atoms:
        if (atom.element.strip() == "O"):
          sps_r = sps.site_symmetry(site_cart=atom.xyz).is_point_group_1()
          if(not sps_r):
            skip = True
            break
      #
      if(not skip):
        if (atoms.size() == 2):
          o_atom = None
          h_atom = None
          for atom in atoms:
            if (atom.element.strip() == "O"): o_atom = atom
            else:                             h_atom = atom
          assert [o_atom, h_atom].count(None) == 0
          h_name = h_atom.name.strip()
          if(len(h_name) == 1):
            atom_name = " %s1 " % h_name
          elif(len(h_name) == 2):
            if(h_name[0].isdigit()):
              if(int(h_name[0]) == 1): atom_name = " %s2 " % h_name[1]
              elif(int(h_name[0]) == 2): atom_name = " %s1 " % h_name[1]
              else: raise RuntimeError
            elif(h_name[1].isdigit()):
              if(int(h_name[1]) == 1): atom_name = " %s2 " % h_name[0]
              elif(int(h_name[1]) == 2): atom_name = " %s1 " % h_name[0]
              else: raise RuntimeError
            else: raise RuntimeError
          else: raise RuntimeError
          insert_atoms(
            atom=o_atom,
            atom_names=[atom_name],
            element=h_atom.element)
        elif (atoms.size() == 1):
          atom = atoms[0]
          assert atom.element.strip() == "O"
          insert_atoms(
            atom=atom,
            atom_names=[" "+element+n+" " for n in ["1","2"]],
            element=element)
    if(neutron):
      xs.switch_to_neutron_scattering_dictionary()
    print("Number of H added:", len(next_to_i_seqs), file=self.log)
    if (len(next_to_i_seqs) == 0): return
    if (self.refinement_flags is not None):
      self.refinement_flags.add(
        next_to_i_seqs=next_to_i_seqs,
        sites_individual = True,
        s_occupancies    = neutron)
    self.reprocess_pdb_hierarchy_inefficient()
    self.idealize_h_minimization()

  def reprocess_pdb_hierarchy_inefficient(self):
    # XXX very inefficient
    """
    Re-process PDB from scratch and create restraints.  Not recommended for
    general use.
    """
    raw_records = self.model_as_pdb()
    pdb_inp = iotbx.pdb.input(source_info=None, lines=raw_records)
    pip = self._pdb_interpretation_params
    pip.pdb_interpretation.clash_guard.max_number_of_distances_below_threshold = 100000000
    pip.pdb_interpretation.clash_guard.max_fraction_of_distances_below_threshold = 1.0
    pip.pdb_interpretation.proceed_with_excessive_length_bonds=True
    pip.pdb_interpretation.clash_guard.nonbonded_distance_threshold=None
    flags = self.refinement_flags
    cs = self.crystal_symmetry()
    log = self.log
    scattering_dict_info = self.scattering_dict_info
    self.all_chain_proxies = None
    self.__init__(
        model_input = pdb_inp,
        crystal_symmetry = cs,
        restraint_objects = self._restraint_objects,
        pdb_interpretation_params = pip,
        process_input = True,
        build_grm = False,
        log = StringIO()
        )
    self.process_input_model(make_restraints=True)
    self.set_refinement_flags(flags)
    if scattering_dict_info is not None:
      self.setup_scattering_dictionaries(
          scattering_table=scattering_dict_info.scattering_table,
          d_min = scattering_dict_info.d_min,
          set_inelastic_form_factors = scattering_dict_info.set_inelastic_form_factors,
          iff_wavelength = scattering_dict_info.iff_wavelength)

  def hd_group_selections(self):
    return utils.combine_hd_exchangable(hierarchy = self._pdb_hierarchy)

  def reset_adp_of_hd_sites_to_be_equal(self):
    scatterers = self._xray_structure.scatterers()
    adp_fl = self.refinement_flags.adp_individual_iso
    adp_fl_a = self.refinement_flags.adp_individual_aniso
    b_iso = self.get_b_iso()
    if(adp_fl is not None):
      for gsel in self.hd_group_selections():
        i,j = gsel[0][0], gsel[1][0]
        element_symbols = \
          [scatterers[i].element_symbol(), scatterers[j].element_symbol()]
        assert element_symbols.count('H') > 0 and element_symbols.count('D')>0
        i_seq_max_q = None
        i_seq_min_q = None
        if(scatterers[i].occupancy < scatterers[j].occupancy):
          i_seq_max_q = j
          i_seq_min_q = i
        else:
          i_seq_max_q = i
          i_seq_min_q = j
        if([adp_fl[i_seq_max_q], adp_fl[i_seq_min_q]].count(True) > 0):
          if([adp_fl_a[i_seq_max_q], adp_fl_a[i_seq_min_q]].count(True) > 0):
            continue
          adp_fl[i_seq_max_q] = True
          adp_fl[i_seq_min_q] = False
          assert [adp_fl[i_seq_max_q], adp_fl[i_seq_min_q]].count(True) > 0
          b_iso[i_seq_min_q] = b_iso[i_seq_max_q]
    self.set_b_iso(values = b_iso)

  def rms_b_iso_or_b_equiv_bonded(self):
    if(self.ias_manager is not None): return 0
    rm = self.restraints_manager
    sc = self.get_xray_structure().sites_cart()
    bs = self.get_xray_structure().extract_u_iso_or_u_equiv()*adptbx.u_as_b(1.)
    return mmtbx.model.statistics.rms_b_iso_or_b_equiv_bonded(
      geometry_restraints_manager = rm,
      sites_cart                  = sc,
      b_isos                      = bs)

  def deep_copy(self):
    return self.select(selection = flex.bool(
      self.get_xray_structure().scatterers().size(), True))

  def add_ias(self, fmodel=None, ias_params=None, file_name=None,
                                                             build_only=False):
    """
    Generate interatomic scatterer pseudo-atoms.
    """
    if(self.ias_manager is not None):
       self.remove_ias()
       fmodel.update_xray_structure(xray_structure = self._xray_structure,
                                    update_f_calc = True)
    print(">>> Adding IAS..........", file=self.log)
    self.old_refinement_flags = None
    if not build_only: self.use_ias = True
    self.ias_manager = ias.manager(
      geometry             = self.restraints_manager.geometry,
      pdb_atoms            = self.get_hierarchy().atoms(),
      xray_structure       = self._xray_structure,
      fmodel               = fmodel,
      params               = ias_params,
      file_name            = file_name,
      log                  = self.log)
    size_all = self._xray_structure.scatterers().size()
    if(not build_only):
      ias_xray_structure = self.ias_manager.ias_xray_structure
      ias_selection = self.get_ias_selection()
      self._xray_structure.concatenate_inplace(other = ias_xray_structure)
      print("Scattering dictionary for combined xray_structure:", file=self.log)
      self._xray_structure.scattering_type_registry().show(out=self.log)
      if(self.refinement_flags is not None):
         self.old_refinement_flags = self.refinement_flags.deep_copy()
         # define flags
         ssites = flex.bool(ias_xray_structure.scatterers().size(), False)
         sadp = flex.bool(ias_xray_structure.scatterers().size(), False)
         # XXX set occ refinement ONLY for involved atoms
         # XXX now it refines only occupancies of IAS !!!
         occupancy_flags = []
         ms = ias_selection.count(False)
         for i in range(1, ias_selection.count(True)+1):
           occupancy_flags.append([flex.size_t([ms+i-1])])
         # set flags
         self.refinement_flags.inflate(
           sites_individual     = ssites,
           s_occupancies        = occupancy_flags,
           adp_individual_iso   = sadp,
           adp_individual_aniso = sadp,
           size_all             = size_all)
         # adjust flags
         if(self.refinement_flags.sites_individual is not None):
           self.refinement_flags.sites_individual.set_selected(ias_selection, False)
           self.refinement_flags.sites_individual.set_selected(~ias_selection, True)
         if(self.refinement_flags.adp_individual_aniso is not None):
           self.refinement_flags.adp_individual_aniso.set_selected(ias_selection, False)
         if(self.refinement_flags.adp_individual_iso is not None):
           self.refinement_flags.adp_individual_iso.set_selected(ias_selection, True)
         #occs = flex.double(self._xray_structure.scatterers().size(), 0.9)
         #self._xray_structure.scatterers().set_occupancies(occs, ~self.ias_selection)
         # D9
         sel = self._xray_structure.scatterers().extract_scattering_types() == "IS9"
         self._xray_structure.convert_to_anisotropic(selection = sel)
         if(self.refinement_flags.adp_individual_aniso is not None):
           self.refinement_flags.adp_individual_aniso.set_selected(sel, True)
         if(self.refinement_flags.adp_individual_iso is not None):
           self.refinement_flags.adp_individual_iso.set_selected(sel, False)
    n_sites = ias_xray_structure.scatterers().size()
    atom_names = []
    for sc in ias_xray_structure.scatterers():
      new_atom_name = sc.label.strip()
      if(len(new_atom_name) < 4): new_atom_name = " " + new_atom_name
      while(len(new_atom_name) < 4): new_atom_name = new_atom_name+" "
      atom_names.append(new_atom_name)
    residue_names = [ "IAS" ] * n_sites
    self._append_pdb_atoms(
      new_xray_structure=ias_xray_structure,
      atom_names=atom_names,
      residue_names=residue_names,
      chain_id=" ",
      i_seq_start=0,
      reset_labels=True)

  def remove_ias(self):
    self.use_ias = False
    ias_selection = self.get_ias_selection()
    if(self.ias_manager is not None):
      self.ias_manager = None
    if(self.old_refinement_flags is not None):
      self.refinement_flags = self.old_refinement_flags.deep_copy()
      self.old_refinement_flags = None
    if(ias_selection is not None):
      self._xray_structure.select_inplace(
        selection = ~ias_selection)
      self._xray_structure.scattering_type_registry().show(out=self.log)
      self._pdb_hierarchy = self._pdb_hierarchy.select(
        atom_selection = ~ias_selection)
      self.get_hierarchy().atoms().reset_i_seq()

  def show_rigid_bond_test(self, out=None, use_id_str=False, prefix=""):
    if (out is None): out = sys.stdout
    scatterers = self._xray_structure.scatterers()
    unit_cell = self._xray_structure.unit_cell()
    rbt_array = flex.double()
    sites_cart = self._xray_structure.sites_cart()
    ias_selection = self.get_ias_selection()
    if ias_selection is not None:#
      sites_cart = sites_cart.select(~ias_selection)
    bond_proxies_simple, asu = self.restraints_manager.geometry.\
        get_all_bond_proxies(sites_cart=sites_cart)
    atoms = self.get_hierarchy().atoms()
    for proxy in bond_proxies_simple:
      i_seqs = proxy.i_seqs
      i,j = proxy.i_seqs
      atom_i = atoms[i]
      atom_j = atoms[j]
      if (    atom_i.element.strip() not in ["H","D"]
          and atom_j.element.strip() not in ["H","D"]):
        sc_i = scatterers[i]
        sc_j = scatterers[j]
        if (sc_i.flags.use_u_aniso() and sc_j.flags.use_u_aniso()):
          p = adp_restraints.rigid_bond_pair(
            sc_i.site, sc_j.site, sc_i.u_star, sc_j.u_star, unit_cell)
          rbt_value = p.delta_z()*10000.
          rbt_array.append(rbt_value)
          name_i, name_j = atom_i.name, atom_j.name
          if (use_id_str):
            name_i = atom_i.id_str()
            name_j = atom_j.id_str()
          print("%s%s %s %10.3f"%(prefix, name_i, name_j, rbt_value), file=out)
    if (rbt_array.size() != 0):
      print("%sRBT values (*10000):" % prefix, file=out)
      print("%s  mean = %.3f" % (prefix, flex.mean(rbt_array)), file=out)
      print("%s  max  = %.3f" % (prefix, flex.max(rbt_array)), file=out)
      print("%s  min  = %.3f" % (prefix, flex.min(rbt_array)), file=out)

  def restraints_manager_energies_sites(self,
        geometry_flags=None,
        custom_nonbonded_function=None,
        compute_gradients=False,
        gradients=None,
        disable_asu_cache=False):
    if(self.restraints_manager is None): return None
    sites_cart = self._xray_structure.sites_cart()
    ias_selection = self.get_ias_selection()
    if(self.use_ias and ias_selection is not None and
       ias_selection.count(True) > 0):
      sites_cart = sites_cart.select(~ias_selection)
    result = self.restraints_manager.energies_sites(
      sites_cart=sites_cart,
      geometry_flags=geometry_flags,
      external_energy_function=None,
      custom_nonbonded_function=custom_nonbonded_function,
      compute_gradients=compute_gradients,
      gradients=gradients,
      disable_asu_cache=disable_asu_cache,
      hd_selection=self.get_hd_selection(),
    )
    return result

  def solvent_selection(self, include_ions=False):
    result = flex.bool()
    get_class = iotbx.pdb.common_residue_names_get_class
    for a in self.get_hierarchy().atoms():
      resname = (a.parent().resname).strip()
      if(get_class(name = resname) == "common_water"):
        result.append(True)
      elif (a.segid.strip() == "ION") and (include_ions):
        result.append(True)
      else: result.append(False)
    return result

  def xray_structure_macromolecule(self):
    sel = self.solvent_selection(include_ions=True)
    if(self.use_ias): sel = sel | self.get_ias_selection()
    result = self._xray_structure.select(~sel)
    return result

  def non_bonded_overlaps(self):
    assert self.has_hd()
    return nbo.info(
      geometry_restraints_manager = self.get_restraints_manager().geometry,
      macro_molecule_selection    = self.selection("protein or nucleotide"),
      sites_cart                  = self.get_sites_cart(),
      hd_sel                      = self.selection("element H or element D"))

  def percent_of_single_atom_residues(self, macro_molecule_only=True):
    # XXX Should be a method of pdb.hierarchy
    sizes = flex.int()
    h = self.get_hierarchy()
    if(macro_molecule_only):
      s = self.selection("protein or nucleotide")
      h = h.select(s)
    for r in h.residue_groups():
      sizes.append(r.atoms().size())
    if(sizes.size()==0): return 0
    return sizes.count(1)*100./sizes.size()

  def select(self, selection):
    # what about 3 types of NCS and self._master_sel?
    # XXX ignores IAS
    if isinstance(selection, flex.size_t):
      selection = flex.bool(self.get_number_of_atoms(), selection)
    new_pdb_hierarchy = self._pdb_hierarchy.select(selection, copy_atoms=True)
    sdi = self.scattering_dict_info
    new_refinement_flags = None
    if(self.refinement_flags is not None):
      new_refinement_flags = self.refinement_flags.select_detached(
        selection = selection)
    new_restraints_manager = None
    if(self.restraints_manager is not None):
      new_restraints_manager = self.restraints_manager.select(selection = selection)
      # XXX is it necessary ?
      # YYY yes, this keeps pair_proxies initialized and available, e.g. for
      # extracting info used in .geo files.
      new_restraints_manager.geometry.pair_proxies(
          sites_cart = self.get_sites_cart().select(selection))
    new_shift_cart = deepcopy(self._shift_cart)
    new_unit_cell_crystal_symmetry = deepcopy(self._unit_cell_crystal_symmetry)
    new_riding_h_manager = None
    if self.riding_h_manager is not None:
      new_riding_h_manager = self.riding_h_manager.select(selection)
    xrs_new = None
    if(self._xray_structure is not None):
      xrs_new = self.get_xray_structure().select(selection)
    sel_tls = self.tls_groups
    if self.tls_groups is not None:
      sel_tls = self.tls_groups.select(selection, self.get_number_of_atoms())
    new = manager(
      model_input                = None,
      crystal_symmetry           = self._crystal_symmetry,
      restraint_objects          = self._restraint_objects,
      monomer_parameters         = self._monomer_parameters,
      expand_with_mtrix          = False,
      pdb_hierarchy              = new_pdb_hierarchy,
      pdb_interpretation_params  = self._pdb_interpretation_params,
      log                        = self.log)
    new.restraints_manager = new_restraints_manager
    new._xray_structure    = xrs_new
    new.tls_groups = sel_tls

    if new_riding_h_manager is not None:
      new.riding_h_manager = new_riding_h_manager
    new.get_xray_structure().scattering_type_registry()
    new.set_refinement_flags(new_refinement_flags)
    new.scattering_dict_info = sdi
    new._update_has_hd()
    # selecting anomalous_scatterer_groups one by one because they are simple list!
    new_anom_groups = []
    for an_gr in self._anomalous_scatterer_groups:
      new_an_gr = an_gr.select(selection)
      if new_an_gr.iselection.size() > 0:
        new_anom_groups.append(new_an_gr)
    new.set_anomalous_scatterer_groups(new_anom_groups)
    new._shift_cart = new_shift_cart
    new._unit_cell_crystal_symmetry = new_unit_cell_crystal_symmetry
    new._processed_pdb_files_srv = self._processed_pdb_files_srv
    if self.ncs_constraints_present():
      new_ncs_groups = self._ncs_groups.select(selection)
      new._ncs_groups = new_ncs_groups
    new._update_master_sel()
    new._mon_lib_srv = self._mon_lib_srv
    new._ener_lib = self._ener_lib
    new._original_model_format = self._original_model_format
    return new

  def number_of_ordered_solvent_molecules(self):
    return self.solvent_selection().count(True)

  def show_groups(self, rigid_body = None, tls = None,
                        out = None, text="Information about rigid groups"):
    global time_model_show
    timer = user_plus_sys_time()
    selections = None
    if(rigid_body is not None):
       selections = self.refinement_flags.sites_rigid_body
    if(tls is not None): selections = self.refinement_flags.adp_tls
    if(self.refinement_flags.sites_rigid_body is None and
                                 self.refinement_flags.adp_tls is None): return
    assert selections is not None
    if (out is None): out = sys.stdout
    print(file=out)
    line_len = len("| "+text+"|")
    fill_len = 80 - line_len-1
    upper_line = "|-"+text+"-"*(fill_len)+"|"
    print(upper_line, file=out)
    next = "| Total number of atoms = %-6d  Number of rigid groups = %-3d                |"
    natoms_total = self._xray_structure.scatterers().size()
    print(next % (natoms_total, len(selections)), file=out)
    print("| group: start point:                        end point:                       |", file=out)
    print("|               x      B  atom  residue  <>        x      B  atom  residue    |", file=out)
    next = "| %5d: %8.3f %6.2f %5s %3s %5s <> %8.3f %6.2f %5s %3s %5s   |"
    sites = self._xray_structure.sites_cart()
    b_isos = self._xray_structure.extract_u_iso_or_u_equiv() * math.pi**2*8
    atoms = self.get_hierarchy().atoms()
    for i_seq, selection in enumerate(selections):
      if (isinstance(selection, flex.bool)):
        i_selection = selection.iselection()
      else:
        i_selection = selection
      start = i_selection[0]
      final = i_selection[i_selection.size()-1]
      first = atoms[start]
      last  = atoms[final]
      first_ag = first.parent()
      first_rg = first_ag.parent()
      last_ag = last.parent()
      last_rg = last_ag.parent()
      print(next % (i_seq+1,
        sites[start][0], b_isos[start],
          first.name, first_ag.resname, first_rg.resid(),
        sites[final][0], b_isos[final],
          last.name, last_ag.resname, last_rg.resid()), file=out)
    print("|"+"-"*77+"|", file=out)
    print(file=out)
    out.flush()
    time_model_show += timer.elapsed()

  def remove_alternative_conformations(self, always_keep_one_conformer):
    # XXX This is not working correctly when something was deleted.
    # Need to figure out a way to update everything so GRM
    # construction will not fail.
    self.geometry_restraints = None
    self._pdb_hierarchy.remove_alt_confs(
        always_keep_one_conformer=always_keep_one_conformer)
    self._pdb_hierarchy.sort_atoms_in_place()
    self._pdb_hierarchy.atoms_reset_serial()
    self.update_xrs()
    self._atom_selection_cache = None
    n_old_atoms = self.get_number_of_atoms()
    n_new_atoms = self.get_number_of_atoms()
    return n_old_atoms - n_new_atoms

  def remove_solvent(self):
    result = self.select(selection = ~self.solvent_selection())
    return result

  def remove_hydrogens(self):
    if self.has_hd():
      noh_selection = self.selection("not (element H or element D)")
      return self.select(noh_selection)
    else:
      return self

  def show_occupancy_statistics(self, out=None, text=""):
    global time_model_show
    timer = user_plus_sys_time()
    # XXX make this more complete and smart
    if(out is None): out = sys.stdout
    print("|-"+text+"-"*(80 - len("| "+text+"|") - 1)+"|", file=out)
    occ = self._xray_structure.scatterers().extract_occupancies()
    # this needs to stay the same size - mask out HD selection
    less_than_zero = (occ < 0.0)
    occ_min = flex.min(occ)
    occ_max = flex.max(occ)
    n_zeros = (occ < 0.1).count(True)
    percent_small = n_zeros * 100. / occ.size()
    n_large = (occ > 2.0).count(True)
    if(percent_small > 30.0):
       print("| *** WARNING: more than 30 % of atoms with small occupancy (< 0.1)       *** |", file=out)
    if(n_large > 0):
       print("| *** WARNING: there are some atoms with large occupancy (> 2.0) ***          |", file=out)
    if(abs(occ_max-occ_min) >= 0.01):
       print("| occupancies: max = %-6.2f min = %-6.2f number of "\
                     "occupancies < 0.1 = %-6d |"%(occ_max,occ_min,n_zeros), file=out)
    else:
       print("| occupancies: max = %-6.2f min = %-6.2f number of "\
                     "occupancies < 0.1 = %-6d |"%(occ_max,occ_min,n_zeros), file=out)
    print("|"+"-"*77+"|", file=out)
    out.flush()
    time_model_show += timer.elapsed()

  def add_solvent(self, solvent_xray_structure,
                        atom_name    = "O",
                        residue_name = "HOH",
                        chain_id     = " ",
                        refine_occupancies = False,
                        refine_adp = None):
    assert refine_adp is not None
    n_atoms = solvent_xray_structure.scatterers().size()
    new_atom_name = atom_name.strip()
    if(len(new_atom_name) < 4): new_atom_name = " " + new_atom_name
    while(len(new_atom_name) < 4): new_atom_name = new_atom_name+" "
    atom_names = [ new_atom_name ] * n_atoms
    residue_names = [ residue_name ] * n_atoms
    nonbonded_types = flex.std_string([ "OH2" ] * n_atoms)
    i_seq = find_common_water_resseq_max(pdb_hierarchy=self._pdb_hierarchy)
    if (i_seq is None or i_seq < 0): i_seq = 0
    self.append_single_atoms(
      new_xray_structure=solvent_xray_structure,
      atom_names=atom_names,
      residue_names=residue_names,
      nonbonded_types=nonbonded_types,
      i_seq_start=i_seq,
      chain_id=chain_id,
      refine_adp=refine_adp,
      refine_occupancies=refine_occupancies,
      reset_labels=True, # Not clear if one forgot to do this or this
      # was not available at the time. Need investigation. Probably will
      # help to eliminate special water treatment in adopt_xray_structure()
      )
    self._update_has_hd()

  def append_single_atoms(self,
      new_xray_structure,
      atom_names,
      residue_names,
      nonbonded_types,
      refine_adp,
      refine_occupancies=None,
      nonbonded_charges=None,
      segids=None,
      i_seq_start = 0,
      chain_id     = " ",
      reset_labels=False):
    assert refine_adp in ["isotropic", "anisotropic"]
    assert new_xray_structure.scatterers().size() == len(atom_names) == \
        len(residue_names) == len(nonbonded_types)
    if segids is not None:
      assert len(atom_names) == len(segids)
    ms = self._xray_structure.scatterers().size() #
    number_of_new_atoms = new_xray_structure.scatterers().size()
    self._xray_structure = \
      self._xray_structure.concatenate(new_xray_structure)
    occupancy_flags = None
    if(refine_occupancies):
      occupancy_flags = []
      for i in range(1, new_xray_structure.scatterers().size()+1):
        occupancy_flags.append([flex.size_t([ms+i-1])])
    if(self.refinement_flags is not None and
       self.refinement_flags.individual_sites):
      ssites = flex.bool(new_xray_structure.scatterers().size(), True)
    else: ssites = None
    # add flags
    if(self.refinement_flags is not None and
       self.refinement_flags.torsion_angles):
      ssites_tors = flex.bool(new_xray_structure.scatterers().size(), True)
    else: ssites_tors = None
    #
    sadp_iso, sadp_aniso = None, None
    if(refine_adp=="isotropic"):
      nxrs_ui = new_xray_structure.use_u_iso()
      if((self.refinement_flags is not None and
          self.refinement_flags.adp_individual_iso) or nxrs_ui.count(True)>0):
        sadp_iso = nxrs_ui
        sadp_aniso = flex.bool(sadp_iso.size(), False)
      else: sadp_iso = None
    if(refine_adp=="anisotropic"):
      nxrs_ua = new_xray_structure.use_u_aniso()
      if((self.refinement_flags is not None and
          self.refinement_flags.adp_individual_aniso) or nxrs_ua.count(True)>0):
        sadp_aniso = nxrs_ua
        sadp_iso = flex.bool(sadp_aniso.size(), False)
      else: sadp_aniso = None
    if(self.refinement_flags is not None):
      self.refinement_flags.inflate(
        sites_individual       = ssites,
        sites_torsion_angles   = ssites_tors,
        adp_individual_iso     = sadp_iso,
        adp_individual_aniso   = sadp_aniso,
        s_occupancies          = occupancy_flags,
        size_all               = ms)#torsion_angles
    #
    self._append_pdb_atoms(
      new_xray_structure=new_xray_structure,
      atom_names=atom_names,
      residue_names=residue_names,
      chain_id=chain_id,
      segids=segids,
      i_seq_start=i_seq_start,
      reset_labels=reset_labels)
   #
    if(self.restraints_manager is not None):
      geometry = self.restraints_manager.geometry
      if (geometry.model_indices is None):
        model_indices = None
      else:
        model_indices = flex.size_t(number_of_new_atoms, 0)
      if (geometry.conformer_indices is None):
        conformer_indices = None
      else:
        conformer_indices = flex.size_t(number_of_new_atoms, 0)
      if (geometry.sym_excl_indices is None):
        sym_excl_indices = None
      else:
        sym_excl_indices = flex.size_t(number_of_new_atoms, 0)
      if (geometry.donor_acceptor_excl_groups is None):
        donor_acceptor_excl_groups = None
      else:
        donor_acceptor_excl_groups = flex.size_t(number_of_new_atoms, 0)
      if (nonbonded_charges is None):
        nonbonded_charges = flex.int(number_of_new_atoms, 0)
      geometry = geometry.new_including_isolated_sites(
        n_additional_sites =number_of_new_atoms,
        model_indices=model_indices,
        conformer_indices=conformer_indices,
        sym_excl_indices=sym_excl_indices,
        donor_acceptor_excl_groups=donor_acceptor_excl_groups,
        site_symmetry_table=new_xray_structure.site_symmetry_table(),
        nonbonded_types=nonbonded_types,
        nonbonded_charges=nonbonded_charges)
      self.restraints_manager = mmtbx.restraints.manager(
        geometry      = geometry,
        cartesian_ncs_manager    = self.restraints_manager.cartesian_ncs_manager,
        normalization = self.restraints_manager.normalization)
      c_ncs_m = self.get_cartesian_NCS_manager()
      if (c_ncs_m is not None):
        c_ncs_m.register_additional_isolated_sites(
          number=number_of_new_atoms)
      self.restraints_manager.geometry.update_plain_pair_sym_table(
        sites_frac = self._xray_structure.sites_frac())
    assert self.size() == self._xray_structure.scatterers().size()
    if self.riding_h_manager is not None:
      new_riding_h_manager = self.riding_h_manager.update(
        pdb_hierarchy       = self._pdb_hierarchy,
        geometry_restraints = geometry,
        n_new_atoms         = number_of_new_atoms)
      self.riding_h_manager = new_riding_h_manager

  def _append_pdb_atoms(self,
      new_xray_structure,
      atom_names,
      residue_names,
      chain_id,
      segids=None,
      i_seq_start=0,
      reset_labels=False):
    """ Add atoms from new_xray_structure to the model in place."""
    assert (len(atom_names) == len(residue_names) ==
            len(new_xray_structure.scatterers()))
    assert (segids is None) or (len(segids) == len(atom_names))
    pdb_model = self._pdb_hierarchy.only_model()
    new_chain = iotbx.pdb.hierarchy.chain(id=chain_id)
    orth = new_xray_structure.unit_cell().orthogonalize
    n_seq = self.size()
    i_seq = i_seq_start
    for j_seq, sc in enumerate(new_xray_structure.scatterers()):
      i_seq += 1
      element, charge = sc.element_and_charge_symbols()
      new_atom = (iotbx.pdb.hierarchy.atom()
        .set_serial(new_serial=iotbx.pdb.hy36encode(width=5, value=n_seq+i_seq))
        .set_name(new_name=atom_names[j_seq])
        .set_xyz(new_xyz=orth(sc.site))
        .set_occ(new_occ=sc.occupancy)
        .set_b(new_b=adptbx.u_as_b(sc.u_iso))
        .set_element(element)
        .set_charge(charge)
        .set_hetero(new_hetero=True))
      if (segids is not None):
        new_atom.segid = segids[j_seq]
      new_atom_group = iotbx.pdb.hierarchy.atom_group(altloc="",
        resname=residue_names[j_seq])
      new_atom_group.append_atom(atom=new_atom)
      new_residue_group = iotbx.pdb.hierarchy.residue_group(
        resseq=iotbx.pdb.resseq_encode(value=i_seq), icode=" ")
      new_residue_group.append_atom_group(atom_group=new_atom_group)
      new_chain.append_residue_group(residue_group=new_residue_group)
    if (new_chain.residue_groups_size() != 0):
      pdb_model.append_chain(chain=new_chain)
    self._update_atom_selection_cache()
    # This deep_copy here for the following reason:
    # sometimes (particualrly in IAS refinement), after update_atom_selection_cache()
    # part of the model atoms loose their parent(). Not clear why.
    # deep_copy seems to help with it.
    self._pdb_hierarchy = self._pdb_hierarchy.deep_copy()
    self.get_hierarchy().atoms().reset_i_seq()
    if (reset_labels):
      self._sync_xrs_labels()
    self.all_chain_proxies = None
    self._processed_pdb_file = None

  def _sync_xrs_labels(self):
    for sc, atom in zip(self.get_xray_structure().scatterers(), self.get_hierarchy().atoms()):
      sc.label = atom.id_str()

  def convert_atom(self,
      i_seq,
      scattering_type,
      atom_name,
      element,
      charge,
      residue_name,
      initial_occupancy=None,
      initial_b_iso=None,
      chain_id=None,
      segid=None,
      refine_occupancies=True,
      refine_adp = None):
    """
    Convert a single atom (usually water) to a different type, including
    adjustment of the xray structure and geometry restraints.
    """
    atom = self.get_hierarchy().atoms()[i_seq]
    atom.name = atom_name
    atom.element = "%2s" % element.strip()
    assert (atom.element.strip() == element)
    if (charge != 0):
      symbol = "+"
      if (charge < 0) : symbol = "-"
      atom.charge = str(abs(charge)) + symbol
    else :
      atom.charge = ""
    atom.parent().resname = residue_name
    if (chain_id is not None):
      assert (len(chain_id) <= 2)
      atom.parent().parent().parent().id = chain_id
    if (segid is not None):
      assert (len(segid) <= 4)
      atom.segid = segid
    scatterer = self._xray_structure.scatterers()[i_seq]
    scatterer.scattering_type = scattering_type
    label = atom.id_str()
    all_labels = [ s.label for s in self._xray_structure.scatterers() ]
    while (label in all_labels):
      rg = atom.parent().parent()
      resseq = rg.resseq_as_int()
      rg.resseq = "%4d" % (resseq + 1)
      label = atom.id_str()
    # scatterer.label = atom.id_str() # will be done below for whole xrs
    if (initial_occupancy is not None):
      # XXX preserve partial occupancies on special positions
      if (scatterer.occupancy != 1.0):
        initial_occupancy = scatterer.occupancy
      scatterer.occupancy = initial_occupancy
      atom.occ = initial_occupancy
    if (initial_b_iso is not None):
      atom.b = initial_b_iso
      scatterer.u_iso = adptbx.b_as_u(initial_b_iso)
    atom_selection = flex.size_t([i_seq])
    if(refine_adp == "isotropic"):
      scatterer.convert_to_isotropic(unit_cell=self._xray_structure.unit_cell())
      if ((self.refinement_flags is not None) and
          (self.refinement_flags.adp_individual_iso is not None)):
        self.refinement_flags.adp_individual_iso.set_selected(atom_selection,
          True)
        if (self.refinement_flags.adp_individual_aniso is not None):
          self.refinement_flags.adp_individual_aniso.set_selected(
            atom_selection, False)
    elif(refine_adp == "anisotropic"):
      scatterer.convert_to_anisotropic(
        unit_cell=self._xray_structure.unit_cell())
      if ((self.refinement_flags is not None) and
          (self.refinement_flags.adp_individual_aniso is not None)):
        self.refinement_flags.adp_individual_aniso.set_selected(atom_selection,
          True)
        if (self.refinement_flags.adp_individual_iso is not None):
          self.refinement_flags.adp_individual_iso.set_selected(atom_selection,
            False)
    if ((self.refinement_flags is not None) and
        (self.refinement_flags.sites_individual is not None)):
      self.refinement_flags.sites_individual.set_selected(atom_selection, True)
    if ((self.refinement_flags is not None) and
        (self.refinement_flags.s_occupancies is not None)):
      flagged = False
      for occgroup in self.refinement_flags.s_occupancies:
        for occsel in occgroup :
          if (i_seq in occsel):
            flagged = True
            break
      if (not flagged):
        self.refinement_flags.s_occupancies.append([atom_selection])
    self.restraints_manager.geometry.update_atom_nonbonded_type(
      i_seq=i_seq,
      nonbonded_type=element,
      charge=charge)
    self._xray_structure.discard_scattering_type_registry()
    assert self.size() == self._xray_structure.scatterers().size()
    self._sync_xrs_labels()
    self.set_xray_structure(self._xray_structure)
    self.get_hierarchy().atoms().reset_i_seq()
    return atom

  def scale_adp(self, scale_max, scale_min):
    b_isos = self._xray_structure.extract_u_iso_or_u_equiv() * math.pi**2*8
    b_isos_mean = flex.mean(b_isos)
    max_b_iso = b_isos_mean * scale_max
    min_b_iso = b_isos_mean / scale_min
    sel_outliers_max = b_isos > max_b_iso
    sel_outliers_min = b_isos < min_b_iso
    b_isos.set_selected(sel_outliers_max, max_b_iso)
    b_isos.set_selected(sel_outliers_min, min_b_iso)
    self.set_b_iso(values = b_isos)

  def get_model_statistics_info(self,
      fmodel_x          = None,
      fmodel_n          = None,
      refinement_params = None):
    if self.model_statistics_info is None:
      self.model_statistics_info = mmtbx.model.statistics.info(
          model             = self,
          fmodel_x          = fmodel_x,
          fmodel_n          = fmodel_n,
          refinement_params = refinement_params)
    return self.model_statistics_info

  def composition(self):
    return mmtbx.model.statistics.composition(
      pdb_hierarchy = self.get_hierarchy())

  def is_neutron(self):
    return self.get_xray_structure().scattering_type_registry().last_table() \
      == "neutron"

  def geometry_statistics(self,
                          use_hydrogens=None,
                          fast_clash=True,
                          condensed_probe=True):
    scattering_table = \
        self.get_xray_structure().scattering_type_registry().last_table()
    rm = self.restraints_manager
    if(rm is None): return None
    if(self.use_ias):
      ias_selection = self.get_ias_selection()
      m = manager(
        model_input        = None,
        pdb_hierarchy      = self.get_hierarchy().select(~ias_selection),
        crystal_symmetry   = self.crystal_symmetry(),
        restraint_objects  = self._restraint_objects,
        monomer_parameters = self._monomer_parameters,
        pdb_interpretation_params = self.get_current_pdb_interpretation_params(),
        log                = null_out())
      m.setup_scattering_dictionaries(scattering_table=scattering_table)
      m.process_input_model(make_restraints=True)
    else:
      m = self.deep_copy()
    m.get_hierarchy().atoms().reset_i_seq()
    hd_selection = m.get_hd_selection()
    if(use_hydrogens==False):
      not_hd_sel = ~hd_selection
      m = m.select(not_hd_sel)
    if(use_hydrogens is None):
      if(self.riding_h_manager is not None or
         scattering_table in ["n_gaussian","wk1995", "it1992", "electron"]):
        not_hd_sel = ~hd_selection
        m = m.select(not_hd_sel)
    size = m.size()
    atoms = m.get_hierarchy().atoms()
    bs = flex.double(size, 10.0)
    atoms.set_b(bs)
    occs = flex.double(size, 1.0)
    atoms.set_occ(occs)
    #
    return mmtbx.model.statistics.geometry(
      model           = m,
      fast_clash      = fast_clash,
      condensed_probe = condensed_probe)

  def occupancy_statistics(self):
    return mmtbx.model.statistics.occupancy(
      hierarchy = self.get_hierarchy())

  def adp_statistics(self):
    rm = self.restraints_manager
    if(self.ias_manager is not None):
      rm = None
    return mmtbx.model.statistics.adp(
      pdb_hierarchy               = self.get_hierarchy(),
      xray_structure              = self.get_xray_structure(),
      use_hydrogens               = False, #XXX
      geometry_restraints_manager = rm)

  def show_adp_statistics(self,
                          out,
                          prefix         = "",
                          padded         = False,
                          pdb_deposition = False):
    self.adp_statistics().show(log = out, prefix = prefix)

  def energies_adp(self, iso_restraints, compute_gradients, use_hd):
    assert self.refinement_flags is not None
    xrs = self._xray_structure
    sel_ = xrs.use_u_iso() | xrs.use_u_aniso()
    selection = sel_
    ias_selection = self.get_ias_selection()
    if(ias_selection is not None and ias_selection.count(True) > 0):
      selection = sel_.set_selected(ias_selection, False)
    n_aniso = 0
    if(self.refinement_flags.adp_individual_aniso is not None):
      n_aniso = self.refinement_flags.adp_individual_aniso.count(True)
    if(n_aniso == 0):
      energies_adp_iso = self.restraints_manager.energies_adp_iso(
        xray_structure    = xrs,
        parameters        = iso_restraints,
        use_u_local_only  = iso_restraints.use_u_local_only,
        use_hd            = use_hd,
        compute_gradients = compute_gradients)
      target = energies_adp_iso.target
    else:
      energies_adp_aniso = self.restraints_manager.energies_adp_aniso(
        xray_structure    = xrs,
        compute_gradients = compute_gradients,
        selection         = selection,
        use_hd            = use_hd)
      target = energies_adp_aniso.target
    u_iso_gradients = None
    u_aniso_gradients = None
    if(compute_gradients):
      if(n_aniso == 0):
        u_iso_gradients = energies_adp_iso.gradients
      else:
        u_aniso_gradients = energies_adp_aniso.gradients_aniso_star
        u_iso_gradients = energies_adp_aniso.gradients_iso
    class result(object):
      def __init__(self):
        self.target = target
        self.u_iso_gradients = u_iso_gradients
        self.u_aniso_gradients = u_aniso_gradients
    return result()

  def is_inside_working_cell(self):
    """
      Return True if all fractional coordinates are inside (0,1)
    """
    sites_frac = self.get_sites_frac()
    min_value=flex.double(sites_frac.min()).min_max_mean().min
    max_value=flex.double(sites_frac.max()).min_max_mean().max
    if min_value >= 0 and max_value <= 1:
      return True
    else:
      return False

  def is_same_model(self, other):
    """
    Return True if models are the same, False otherwise.
    XXX Can be endlessly fortified.
    """
    f0 = self.size() == other.size()
    f1 = self.get_hierarchy().is_similar_hierarchy(other.get_hierarchy())
    f2 = self.get_xray_structure().is_similar(other.get_xray_structure())
    x1 = self.get_hierarchy().extract_xray_structure(
      crystal_symmetry = self.crystal_symmetry())
    x2 = other.get_hierarchy().extract_xray_structure(
      crystal_symmetry = other.crystal_symmetry())
    f3 = x1.is_similar(x2)
    f = list(set([f0,f1,f2,f3]))
    return len(f)==1 and f[0]

  def set_refine_individual_sites(self, selection = None):
    self._xray_structure.scatterers().flags_set_grads(state=False)
    if(selection is None):
      if(self.refinement_flags is not None):
        selection = self.refinement_flags.sites_individual
    if(selection is not None):
      self._xray_structure.scatterers().flags_set_grad_site(
        iselection = selection.iselection())

  def set_refine_individual_adp(self, selection_iso = None,
                                      selection_aniso = None):
    self._xray_structure.scatterers().flags_set_grads(state=False)
    if(selection_iso is None):
      selection_iso = self.refinement_flags.adp_individual_iso
    if(selection_iso is not None):
      self._xray_structure.scatterers().flags_set_grad_u_iso(
        iselection = selection_iso.iselection())
    if(selection_aniso is None):
      selection_aniso = self.refinement_flags.adp_individual_aniso
    if(selection_aniso is not None):
      self._xray_structure.scatterers().flags_set_grad_u_aniso(
        iselection = selection_aniso.iselection())

  def _expand_symm_helper(self, records_container):
    """
    This will expand hierarchy and ss annotations. In future anything else that
    should be expanded have to be added here. e.g. TLS.
    LIMITATION: ANISOU records in resulting hierarchy will be invalid!!!
    """
    from iotbx.pdb.utils import all_chain_ids
    roots=[]
    all_cids = all_chain_ids()
    duplicate_prevention = {}
    chain_ids_match_dict = {} # {'old chain id': [new ids]}
    for m in self.get_hierarchy().models():
      for c in m.chains():
        chain_id_key = "%s%s" % (m.id, c.id)
        if chain_id_key in duplicate_prevention:
          continue
        duplicate_prevention[chain_id_key] = False
        chain_ids_match_dict[c.id] = []
        cid = c.id
        try:
          ind = all_cids.index(cid)
        except ValueError:
          ind = -1
        if ind >= 0:
          del all_cids[ind]
    cid_counter = 0
    for r,t in zip(records_container.r, records_container.t):
      leave_chain_ids = False
      if r.is_r3_identity_matrix() and t.is_col_zero():
        leave_chain_ids = True
      for mm in self.get_hierarchy().models():
        root = iotbx.pdb.hierarchy.root()
        m = iotbx.pdb.hierarchy.model()
        for k in duplicate_prevention.keys():
          duplicate_prevention[k] = False
        for c in mm.chains():
          c = c.detached_copy()
          if not leave_chain_ids: # and not duplicate_prevention["%s%s" % (mm.id, c.id)]:
            if duplicate_prevention["%s%s" % (mm.id, c.id)]:
              if len(chain_ids_match_dict[c.id]) > 0:
                new_cid = chain_ids_match_dict[c.id][-1]
              else:
                new_cid = c.id
            else:
              new_cid = all_cids[cid_counter]
              cid_counter += 1
              chain_ids_match_dict[c.id].append(new_cid)
              duplicate_prevention["%s%s" % (mm.id, c.id)] = True
            c.id = new_cid
          xyz = c.atoms().extract_xyz()
          new_xyz = r.elems*xyz+t
          c.atoms().set_xyz(new_xyz)
          m.append_chain(c)
        root.append_model(m)
        roots.append(root)
    result = iotbx.pdb.hierarchy.root()
    for rt in roots:
      result.transfer_chains_from_other(other=rt)
    #validation
    vals = list(chain_ids_match_dict.values())
    for v in vals:
      assert len(vals[0]) == len(v), chain_ids_match_dict
    result.reset_i_seq_if_necessary()
    self._pdb_hierarchy = result
    if self._pdb_hierarchy.models_size() == 1:
      # Drop model.id if there is only one model.
      # Otherwise there are problems with set_xray_structure...
      self._pdb_hierarchy.only_model().id = ""
    # Now deal with SS annotations
    ssa = self.get_ss_annotation()
    if ssa is not None:
      ssa.multiply_to_asu_2(chain_ids_match_dict)
      self.set_ss_annotation(ssa)
    # reset
    self.get_hierarchy().atoms().reset_i_seq()
    self._xray_structure = None
    self._all_chain_proxies = None
    self._update_atom_selection_cache()
    self.restraints_manager = None

  def _biomt_mtrix_container_is_good(self, records_container):
    if(records_container is None): return False
    if(len(records_container.r)==0): return False
    if len(records_container.r)==1:
      r, t = records_container.r[0], records_container.t[0]
      if r.is_r3_identity_matrix() and t.is_col_zero():
        return False
    if (records_container.is_empty() or
        len(records_container.r) == 0 or
        records_container.validate()):
      return False
    return True

  def mtrix_expanded(self):
    return self._mtrix_expanded

  def biomt_expanded(self):
    return self._biomt_expanded

  def expand_with_MTRIX_records(self):
    if(self.mtrix_expanded()): return
    if(not self._biomt_mtrix_container_is_good(self.mtrix_operators)):
      return
    if(self.get_hierarchy() is None and self.get_model_input() is not None):
      self._pdb_hierarchy = deepcopy(self._model_input).construct_hierarchy(
          self._pdb_interpretation_params.pdb_interpretation.sort_atoms)
    self._expand_symm_helper(self.mtrix_operators)
    self._mtrix_expanded = True

  def expand_with_BIOMT_records(self):
    """
    expanding current hierarchy and ss_annotations with BIOMT matrices.
    Known limitations: will expand everything, regardless of what selections
    were setted in BIOMT header.
    """
    if(self.biomt_expanded()): return
    if not self._biomt_mtrix_container_is_good(self.biomt_operators):
      return
    # Check if BIOMT and MTRIX are identical and then do not apply BIOMT
    br = self.biomt_operators.r
    bt = self.biomt_operators.t
    mr = self.mtrix_operators.r
    mt = self.mtrix_operators.t
    if(len(br)==len(mr) and len(bt)==len(mt)):
      cntr1=0
      for bri in br:
        for mri in mr:
          if((bri-mri).is_approx_zero(eps=1.e-4)):
            cntr1+=1
            break
      cntr2=0
      for bti in bt:
        for mti in mt:
          if((bti-mti).is_approx_zero(eps=1.e-4)):
            cntr2+=1
            break
      if(cntr1==len(br) and cntr2==len(bt)): return
    #
    self._expand_symm_helper(self.biomt_operators)
    self._biomt_expanded = True

  def set_sequences(self, sequences, custom_residues=None,
                    similarity_matrix=None, min_allowable_identity=None,
                    minimum_identity=0.5):
    """
    Set the canonical sequence for the model. This should be all the
    protein and nucleic acid residues expected in the crystal, not just
    the ones in the modeled structure.

    Parameters
    ----------
    sequences: list of sequences
      list of iotbx.bioinformatics.sequence objects. The output from the
      get_sequence function of the DataManager will work.
    custom_residues: list of str
      List of custom 3-letter residues to keep in pdbx_one_letter_sequence
      The 3-letter residue must exist in the model
    similarity_matrix: blosum50 dayhoff *identity
      choice from mmtbx.validation.sequence.master_phil
    min_allowable_identity: float
      parameter from mmtbx.validation.sequence.master_phil
    minimum_identity: float
      parameter from mmtbx.validation.sequence.validation
      minimum identity to match

    Returns
    -------
    Nothing
    """
    if isinstance(sequences, sequence):
      sequences = [sequences]
    for seq in sequences:
      if not isinstance(seq, sequence):
        raise Sorry("A non-sequence object was found.")

    # match sequence with chain
    params = sequence_master_phil.extract()
    if similarity_matrix is not None:
      params.similarity_matrix = similarity_matrix
    if min_allowable_identity is not None:
      params.min_allowable_identity = min_allowable_identity
    self._sequence_validation = sequence_validation(
      pdb_hierarchy=self._pdb_hierarchy,
      sequences=sequences,
      custom_residues=custom_residues,
      params=params,
      log=self.log,
      minimum_identity=minimum_identity
    )
