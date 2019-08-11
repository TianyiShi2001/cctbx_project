from __future__ import absolute_import, division, print_function
import iotbx.pdb
import mmtbx.model
import math, sys, os
from libtbx import group_args
from scitbx.array_family import flex
from libtbx.test_utils import approx_equal
from mmtbx.secondary_structure import find_ss_from_ca
from libtbx.utils import null_out
import libtbx.load_env
from libtbx import easy_pickle
from cctbx import uctbx
from mmtbx.utils import run_reduce_with_timeout

import numpy as np # XXX See if I can avoid it!


def get_pair_generator(crystal_symmetry, buffer_thickness, sites_cart):
  sst = crystal_symmetry.special_position_settings().site_symmetry_table(
    sites_cart = sites_cart)
  from cctbx import crystal
  conn_asu_mappings = crystal_symmetry.special_position_settings().\
    asu_mappings(buffer_thickness = buffer_thickness)
  conn_asu_mappings.process_sites_cart(
    original_sites      = sites_cart,
    site_symmetry_table = sst)
  conn_pair_asu_table = crystal.pair_asu_table(asu_mappings = conn_asu_mappings)
  conn_pair_asu_table.add_all_pairs(distance_cutoff = buffer_thickness)
  pair_generator = crystal.neighbors_fast_pair_generator(
    conn_asu_mappings, distance_cutoff = buffer_thickness)
  return group_args(
    pair_generator    = pair_generator,
    conn_asu_mappings = conn_asu_mappings)

def apply_symop_to_copy(atom, rt_mx_ji, fm, om):
  atom = atom.detached_copy()
  t1 = fm*flex.vec3_double([atom.xyz])
  t2 = rt_mx_ji*t1[0]
  t3 = om*flex.vec3_double([t2])
  atom.set_xyz(t3[0])
  return atom

def make_atom_id(atom, index):
  return group_args(
    id_str = atom.id_str().replace("pdb=",""),
    index  = index,
    name   = atom.name,
    b      = atom.b,
    occ    = atom.occ,
    chain  = atom.parent().parent().parent().id,
    resseq = atom.parent().parent().resseq,
    altloc = atom.parent().altloc)

def get_stats(data):
  if(data.size()<50): return None
  mean=data.min_max_mean().mean
  sd=data.standard_deviation_of_the_sample()
  x=data-mean
  skew=(x**3).min_max_mean().mean/sd**3
  kurtosis=(x**4).min_max_mean().mean/sd**4
  return group_args(mean=mean, sd=sd, skew=skew, kurtosis=kurtosis)

# XXX None at the moment
master_phil_str = '''
hbond {

}
'''

def show_histogram(data, n_slots, data_min, data_max, log=sys.stdout):
  from cctbx.array_family import flex
  h_data = flex.double()
  hm = flex.histogram(
    data=data, n_slots=n_slots, data_min=data_min, data_max=data_max)
  lc_1 = hm.data_min()
  s_1 = enumerate(hm.slots())
  for (i_1,n_1) in s_1:
    hc_1 = hm.data_min() + hm.slot_width() * (i_1+1)
    #print >> log, "%10.5f - %-10.5f : %d" % (lc_1, hc_1, n_1)
    #print >> log, "%10.2f : %d" % ((lc_1+hc_1)/2, n_1)
    print ("%10.2f : %10.4f" % ((lc_1+hc_1)/2, n_1*100./data.size()), file=log)
    lc_1 = hc_1
  return h_data

def stats(model, prefix):
  # Get rid of H, multi-model, no-protein and single-atom residue models
  if(model.percent_of_single_atom_residues()>20):
    return None
  sel = model.selection(string = "protein")
  if(sel.count(True)==0):
    return None
  ssr = "protein and not (element H or element D or resname UNX or resname UNK or resname UNL)"
  sel = model.selection(string = ssr)
  model = model.select(sel)
  if(len(model.get_hierarchy().models())>1):
    return None
  # Add H; this looses CRYST1 !
  rr = run_reduce_with_timeout(
    stdin_lines = model.get_hierarchy().as_pdb_string().splitlines(),
    file_name   = None,
    parameters  = "-oh -his -flip -keep -allalt -pen9999 -",
    override_auto_timeout_with=None)
  # Create model; this is a single-model pure protein with new H added
  pdb_inp = iotbx.pdb.input(source_info = None, lines = rr.stdout_lines)
  model = mmtbx.model.manager(
    model_input      = None,
    build_grm        = True,
    pdb_hierarchy    = pdb_inp.construct_hierarchy(),
    process_input    = True,
    log              = null_out())
  box = uctbx.non_crystallographic_unit_cell_with_the_sites_in_its_center(
    sites_cart   = model.get_sites_cart(),
    buffer_layer = 5)
  model.set_sites_cart(box.sites_cart)
  model._crystal_symmetry = box.crystal_symmetry()
  # Get SS annotations
  SS = find_ss_from_ca.find_secondary_structure(
    hierarchy   = model.get_hierarchy(),
    #ss_by_chain = False, # enabling will make it slow.
    out         = null_out())
  # Convert SS annotations into bool selections
  alpha_sel = SS.annotation.overall_helix_selection().strip()
  beta_sel  = SS.annotation.overall_sheet_selection().strip()
  if(len(alpha_sel)==0 or alpha_sel=="()"): alpha_sel=None
  if(len(beta_sel) ==0 or beta_sel =="()"):  beta_sel=None
  if([alpha_sel, beta_sel].count(None)==0):
    alpha_sel = model.selection(string="%s"%alpha_sel)
    beta_sel  = model.selection(string="%s"%beta_sel)
    loop_sel  = ~(alpha_sel | beta_sel)
  elif(alpha_sel is not None):
    alpha_sel = model.selection(string="%s"%alpha_sel)
    loop_sel = ~alpha_sel
  elif(beta_sel is not None):
    beta_sel  = model.selection(string="%s"%beta_sel)
    loop_sel  = ~beta_sel
  else:
    loop_sel = model.selection(string="all")
  # Get individual stats
  def get_selected(sel):
    result = None
    if(type(sel)==str and sel=="all"):
      return find(model = model, a_DHA_cutoff=90).get_params_as_arrays()
    elif(sel is not None and sel.count(True)>0):
      result = find(
        model = model.select(sel), a_DHA_cutoff=90).get_params_as_arrays()
    return result
  result_dict = {}
  result_dict["all"]   = get_selected(sel="all")
  result_dict["alpha"] = get_selected(sel=alpha_sel)
  result_dict["beta"]  = get_selected(sel=beta_sel)
  result_dict["loop"]  = get_selected(sel=loop_sel)
  # Load histograms for reference high-resolution d_HA and a_DHA
  pkl_fn = libtbx.env.find_in_repositories(
    relative_path="mmtbx")+"/nci/d_HA_and_a_DHA_high_res.pkl"
  assert os.path.isfile(pkl_fn)
  ref = easy_pickle.load(pkl_fn)
  #
  import matplotlib as mpl
  mpl.use('Agg')
  import matplotlib.pyplot as plt
  fig = plt.figure(figsize=(15,15))
  kwargs = dict(histtype='bar', bins=20, range=[1.6,3.0], alpha=.8)
  for i, key in enumerate(["alpha", "beta", "loop", "all"]):
    ax = plt.subplot(int("42%d"%(i+1)))
    HB = result_dict[key]
    if HB is None: continue
    w1 = np.ones_like(HB.d_HA)/HB.d_HA.size()
    ax.hist(HB.d_HA, color="orangered", weights=w1, rwidth=0.3, **kwargs)
    ax.set_title("Distance (%s)"%key)
    bins = list(flex.double(ref.distances[key].bins))
    ax.bar(bins, ref.distances[key].vals, alpha=.3, width=0.07)
  #
  kwargs = dict(histtype='bar', bins=20, range=[90,180], alpha=.8)
  for j, key in enumerate(["alpha", "beta", "loop", "all"]):
    ax = plt.subplot(int("42%d"%(i+j+2)))
    HB = result_dict[key]
    if HB is None: continue
    w1 = np.ones_like(HB.a_DHA)/HB.a_DHA.size()
    ax.hist(HB.a_DHA, color="orangered", weights=w1, rwidth=0.3, **kwargs)
    ax.set_title("Angle (%s)"%key)
    ax.bar(ref.angles[key].bins, ref.angles[key].vals, width=4.5, alpha=.3)

  fig.savefig("%s.png"%prefix, dpi=100)


def precheck(atoms, i, j, Hs, As, Ds, fsc0):
  """
  Check if two atoms are potential H bond partners, based on element and altloc
  """
  ei, ej = atoms[i].element, atoms[j].element
  altloc_i = atoms[i].parent().altloc
  altloc_j = atoms[j].parent().altloc
  resseq_i = atoms[i].parent().parent().resseq
  resseq_j = atoms[j].parent().parent().resseq
  one_is_Hs = ei in Hs or ej in Hs
  other_is_acceptor = ei in As or ej in As
  is_candidate = one_is_Hs and other_is_acceptor and \
    altloc_i == altloc_j and resseq_i != resseq_j
  if(ei in Hs):
    bound_to_h = fsc0[i]
    if(not bound_to_h): # exclude 'lone' H
      is_candidate = False
    elif(atoms[bound_to_h[0]].element not in Ds): # Use only first atom bound to H
      is_candidate = False
  if(ej in Hs):
    bound_to_h = fsc0[j]
    if(not bound_to_h):
      is_candidate = False
    elif(atoms[bound_to_h[0]].element not in Ds):
      is_candidate = False
  return is_candidate

def get_D_H_A_Y(p, Hs, fsc0, rt_mx_ji, fm, om, atoms):
  """
  Get atom objects for donor and acceptor atoms
  Apply symmetry op if necessary, so that correct geometry can be calculated
  """
  i, j = p.i_seq, p.j_seq
  Y = []
  if(atoms[i].element in Hs):
    H = atoms[i]
    D = atoms[fsc0[i][0]]
    A = atoms[j]
    Y_iseqs = fsc0[j]
    if(len(Y_iseqs)>0):
      Y = [atoms[k] for k in fsc0[j]]
    atom_H = make_atom_id(atom = H, index = i)
    atom_A = make_atom_id(atom = A, index = j)
    atom_D = make_atom_id(atom = D, index = D.i_seq)
    if(rt_mx_ji is not None and str(rt_mx_ji) != "x,y,z"):
      A = apply_symop_to_copy(A, rt_mx_ji, fm, om)
      if(len(Y_iseqs)>0):
        Y = [apply_symop_to_copy(y, rt_mx_ji, fm, om) for y in Y]
  if(atoms[j].element in Hs):
    H = atoms[j]
    D = atoms[fsc0[j][0]]
    A = atoms[i]
    Y_iseqs = fsc0[i]
    if(len(Y_iseqs)>0):
      Y = [atoms[k] for k in fsc0[i]]
    atom_A = make_atom_id(atom = A, index = i)
    atom_H = make_atom_id(atom = H, index = j)
    atom_D = make_atom_id(atom = D, index = D.i_seq)
    if(rt_mx_ji is not None and str(rt_mx_ji) != "x,y,z"):
      H = apply_symop_to_copy(H, rt_mx_ji, fm, om)
      D = apply_symop_to_copy(D, rt_mx_ji, fm, om)
  return D, H, A, Y, atom_A, atom_H, atom_D

class find(object):
  """
     Y
      \
       A
        .
         .
         H
         |
         D
        / \

    A = O, N, S
    D = O, N, S
    90 <= a_YAH <= 180
    a_DHA >= 120
    1.4 <= d_HA <= 3.0
    2.5 <= d_DA <= 3.5
  """
  def __init__(self,
        model,
        Hs           = ["H", "D"],
        As           = ["O","N","S","F","CL"],
        Ds           = ["O","N","S"],
        d_HA_cutoff  = [1.4, 3.0], # original: [1.4, 2.4],
        d_DA_cutoff  = [2.5, 4.1], # not used
        a_DHA_cutoff = 120,        # should be greater than this
        a_YAH_cutoff = [90, 180],  # should be within this interval
        protein_only = False,
        pair_proxies = None):
    self.result = []
    self.model = model
    self.pair_proxies = pair_proxies
    self.external_proxies = False
    if(self.pair_proxies is not None):
      self.external_proxies = True
    atoms = self.model.get_hierarchy().atoms()
    geometry = self.model.get_restraints_manager()
    fsc0 = geometry.geometry.shell_sym_tables[0].full_simple_connectivity()
    bond_proxies_simple, asu = geometry.geometry.get_all_bond_proxies(
      sites_cart = self.model.get_sites_cart())
    sites_cart = self.model.get_sites_cart()
    crystal_symmetry = self.model.crystal_symmetry()
    fm = crystal_symmetry.unit_cell().fractionalization_matrix()
    om = crystal_symmetry.unit_cell().orthogonalization_matrix()
    pg = get_pair_generator(
      crystal_symmetry = crystal_symmetry,
      buffer_thickness = d_HA_cutoff[1],
      sites_cart       = sites_cart)
    get_class = iotbx.pdb.common_residue_names_get_class
    # find proxies if not provided
    if(self.pair_proxies is None):
      pp = []
      self.pair_proxies = []
      pp = [p for p in pg.pair_generator]
    else:
      pp = self.pair_proxies
    # now loop over proxies
    for p in pp:
      i, j = p.i_seq, p.j_seq
      if(self.external_proxies): # making sure proxies point to same atoms
        a_i = make_atom_id(atom = atoms[i], index = i).id_str
        a_j = make_atom_id(atom = atoms[j], index = j).id_str
        assert a_i == p.atom_A.id_str, [a_i, p.atom_A.id_str]
        assert a_j == p.atom_H.id_str, [a_j, p.atom_H.id_str]
      # presecreen candidates
      ei, ej = atoms[i].element, atoms[j].element
      is_candidate = precheck(
        atoms = atoms,
        i = i,
        j = j,
        Hs = Hs,
        As = As,
        Ds = Ds,
        fsc0 = fsc0)
      if(protein_only):
        for it in [i,j]:
          resname = atoms[it].parent().resname
          is_candidate &= get_class(name=resname) == "common_amino_acid"
      if(not is_candidate): continue
      # pre-screen candidates end
      # symop tp map onto symmetry related
      rt_mx_ji = None
      if(not self.external_proxies):
        rt_mx_i = pg.conn_asu_mappings.get_rt_mx_i(p)
        rt_mx_j = pg.conn_asu_mappings.get_rt_mx_j(p)
        rt_mx_ji = rt_mx_i.inverse().multiply(rt_mx_j)
      else:
        rt_mx_ji = p.rt_mx_ji
      #
      D, H, A, Y, atom_A, atom_H, atom_D = get_D_H_A_Y(
        p        = p,
        Hs       = Hs,
        fsc0     = fsc0,
        rt_mx_ji = rt_mx_ji,
        fm       = fm,
        om       = om,
        atoms    = atoms)
      if(len(Y) == 0): continue # don't use 'lone' acceptors
      d_HA = A.distance(H)
      if(not self.external_proxies):
        assert d_HA <= d_HA_cutoff[1]
        assert approx_equal(math.sqrt(p.dist_sq), d_HA, 1.e-3)
#      assert H.distance(D) < 1.15, [H.distance(D), H.name, D.name]
      # filter by a_DHA
      a_DHA = H.angle(A, D, deg=True)
      if(not self.external_proxies):
        if(a_DHA < a_DHA_cutoff): continue
      # filter by a_YAH
      a_YAH = []
      if(len(Y)>0):
        for Y_ in Y:
          a_YAH_ = A.angle(Y_, H, deg=True)
          a_YAH.append(a_YAH_)
      if(not self.external_proxies):
        flags = []
        for a_YAH_ in a_YAH:
          flags.append(
            not (a_YAH_ >= a_YAH_cutoff[0] and a_YAH_ <= a_YAH_cutoff[1]))
        flags = list(set(flags))
        if(len(flags)>1 or (len(flags)==1 and flags[0])): continue
      #
      assert approx_equal(d_HA, H.distance(A), 1.e-3)
      self.result.append(group_args(
        i       = i,
        j       = j,
        atom_H  = atom_H,
        atom_A  = atom_A,
        atom_D  = atom_D,
        symop   = rt_mx_ji,
        d_HA    = d_HA,
        a_DHA   = a_DHA,
        a_YAH   = a_YAH,
        d_AD    = A.distance(D)
      ))
      if(not self.external_proxies):
        proxy_custom = group_args(i_seq = i, j_seq = j, rt_mx_ji = rt_mx_ji,
          atom_H = atom_H, atom_A = atom_A)
        self.pair_proxies.append(proxy_custom)
    #
    self.as_restraints()

  def get_params_as_arrays(self, b=None, occ=None):
    d_HA  = flex.double()
    a_DHA = flex.double()
    a_YAH = flex.double()
    for r in self.result:
      if(b   is not None and r.atom_H.b>b): continue
      if(b   is not None and r.atom_A.b>b): continue
      if(occ is not None and r.atom_H.occ<occ): continue
      if(occ is not None and r.atom_A.occ<occ): continue
      d_HA .append(r.d_HA )
      a_DHA.append(r.a_DHA)
      if(len(r.a_YAH)>0):
        a_YAH.extend(flex.double(r.a_YAH))
    return group_args(d_HA=d_HA, a_DHA=a_DHA, a_YAH=a_YAH)

  def get_counts(self):
    data_theta_1_all = flex.double()
    data_theta_1_fil = flex.double()
    data_theta_2_all = flex.double()
    data_theta_2_fil = flex.double()
    data_d_HA_all = flex.double()
    data_d_HA_fil = flex.double()
    n_sym = 0
    for r in self.result:
      if(str(r.symop) != "x,y,z"):
        n_sym += 1
      data_theta_1_all.append(r.a_DHA)
      data_theta_2_all.extend(flex.double(r.a_YAH))
      data_d_HA_all.append(r.d_HA)
      if(r.atom_H.b>30):    continue
      if(r.atom_A.b>30):    continue
      if(r.atom_H.occ<0.9): continue
      if(r.atom_A.occ<0.9): continue
      data_theta_1_fil.append(r.a_DHA)
      data_theta_2_fil.extend(flex.double(r.a_YAH))
      data_d_HA_fil.append(r.d_HA)
    theta_1 = group_args(
      overall  = get_stats(data_theta_1_all),
      filtered = get_stats(data_theta_1_fil))
    theta_2 = group_args(
      overall  = get_stats(data_theta_2_all),
      filtered = get_stats(data_theta_2_fil))
    d_HA = group_args(
      overall  = get_stats(data_d_HA_all),
      filtered = get_stats(data_d_HA_fil))
    bpr=float(len(self.result))/\
      len(list(self.model.get_hierarchy().residue_groups()))
    return group_args(
      theta_1 = theta_1,
      theta_2 = theta_2,
      d_HA    = d_HA,
      n       = len(self.result),
      n_sym   = n_sym,
      bpr     = bpr)

  def show_summary(self, log = sys.stdout):
    def printit(o,f):
      fmt="%7.3f %7.3f %7.3f %7.3f"
      if(o is not None):
        print("  overall : "+fmt%(o.mean, o.sd, o.skew, o.kurtosis), file=log)
      if(f is not None):
        print("  filtered: "+fmt%(f.mean, f.sd, f.skew, f.kurtosis), file=log)
    c = self.get_counts()
    print("Total:       %d"%c.n,     file=log)
    print("Symmetry:    %d"%c.n_sym, file=log)
    print("Per residue: %7.4f"%c.bpr,   file=log)
    print("               Mean      SD    Skew   Kurtosis",   file=log)
    print("theta_1:",   file=log)
    o, f = c.theta_1.overall, c.theta_1.filtered
    printit(o,f)
    print("theta_2:",   file=log)
    o, f = c.theta_2.overall, c.theta_2.filtered
    printit(o,f)
    print("d_HA:",   file=log)
    o, f = c.d_HA.overall, c.d_HA.filtered
    printit(o,f)

  def show(self, log = sys.stdout, sym_only=False):
    for r in self.result:
      ids_i = r.atom_H.id_str
      ids_j = r.atom_A.id_str
      if(sym_only):
        if(str(r.symop)=="x,y,z"): continue
      print("%4d %4d"%(r.i,r.j), "%s<>%s"%(ids_i, ids_j), \
        "d_HA=%5.3f"%r.d_HA, "d_AD=%5.3f"%r.d_AD, "a_DHA=%7.3f"%r.a_DHA, \
        "symop: %s"%str(r.symop), " ".join(["a_YAH=%d"%i for i in r.a_YAH]),
        file=log)

  def as_pymol(self, prefix="hbonds_pymol"):
    pdb_file_name = "%s.pdb"%prefix
    with open(pdb_file_name, "w") as of:
      print(self.model.model_as_pdb(), file=of)
    with open("%s.pml"%prefix, "w") as of:
      print("load", "/".join([os.getcwd(), pdb_file_name]), file=of)
      for r in self.result:
        if(str(r.symop) != "x,y,z"): continue
        ai = r.atom_H
        aj = r.atom_A
        one = "chain %s and resi %s and name %s and alt '%s'"%(
          ai.chain, ai.resseq, ai.name, ai.altloc)
        two = "chain %s and resi %s and name %s and alt '%s'"%(
          aj.chain, aj.resseq, aj.name, aj.altloc)
        print("dist %s, %s"%(one, two), file=of)

  def as_restraints(self, file_name="hbond.eff", distance_ideal=None, sigma_dist=0.1,
       angle_ideal = None, sigma_angle=2, use_actual=True):
    f = "chain %s and resseq %s and name %s"
    with open(file_name, "w") as of:
      print("geometry_restraints.edits {", file=of)
      for r in self.result:
        h = f%(r.atom_H.chain, r.atom_H.resseq, r.atom_H.name)
        a = f%(r.atom_A.chain, r.atom_A.resseq, r.atom_A.name)
        d = f%(r.atom_D.chain, r.atom_D.resseq, r.atom_D.name)
        if(not use_actual):
          if(r.d_HA<2.5): dt = 2.05
          else:           dt = 2.8
          if(r.a_DHA<130): at = 115
          else:            at = 160
        else:
          dt = r.d_HA
          at = r.a_DHA
        dis = """    bond {
          atom_selection_1 = %s
          atom_selection_2 = %s
          symmetry_operation = %s
          distance_ideal = %f
          sigma = 0.05
         }"""%(h,a,str(r.symop),dt)
        if(str(r.symop)!="x,y,z"): continue
        ang = """    angle {
          atom_selection_1 = %s
          atom_selection_2 = %s
          atom_selection_3 = %s
          angle_ideal = %f
          sigma = 5
          }"""%(a,h,d,at)
        print(dis, file=of)
        print(ang, file=of)
      print("}", file=of)
