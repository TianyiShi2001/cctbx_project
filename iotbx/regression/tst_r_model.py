from __future__ import absolute_import, division, print_function
from cctbx.array_family import flex
import os, sys
from libtbx.utils import Sorry
from libtbx.test_utils import approx_equal
from mmtbx.model import manager as model_manager

def exercise(file_name, out = sys.stdout):
  if not os.path.isfile(file_name):
    raise Sorry("Missing the file: %s" %(file_name)+"\n"+"Please run with phenix.python read_write_mrc.py my_mrcfile.mrc")

  print ("Reading from %s" %(file_name))
  from iotbx.map_manager import map_manager

  m = map_manager(file_name)

  # make a little model
  sites_cart = flex.vec3_double( ((8, 10, 12), (14, 15, 16)))
  model = model_manager.from_sites_cart(
         atom_name = ' CA ',
         resname = 'ALA',
         chain_id = 'A',
         b_iso = 30.,
         occ = 1.,
         scatterer = 'C',
         sites_cart = sites_cart,
         crystal_symmetry = m.crystal_symmetry())

  # make a map_model_manager with lots of maps and model and ncs
  from iotbx.map_model_manager import map_model_manager

  from mmtbx.ncs.ncs import ncs
  ncs_object=ncs()
  ncs_object.set_unit_ncs()
  mam = map_model_manager(
          map_manager =  m,
          ncs_object =  ncs_object,
          map_manager_1 =  m.deep_copy(),
          map_manager_2 =  m.deep_copy(),
          extra_map_manager_list =  [m.deep_copy(),m.deep_copy(),m.deep_copy()],
          extra_map_manager_id_list = ["extra_1","extra_2","map_manager_mask"],
          model     = model.deep_copy(),)
  r_model=mam.as_r_model()

  print (r_model.map_manager())
  print (r_model.model())
  print (r_model.map_manager_1())
  print (r_model.map_manager_2())
  print (r_model.map_manager_mask())
  print (r_model.map_manager().ncs_object())
  all_map_names=r_model.map_manager_id_list()
  for id in all_map_names:
    print("Map_manager %s: %s " %(id,r_model.get_map_manager(id)))

  # Make a deep_copy
  dc=r_model.deep_copy()
  new_r_model=r_model.deep_copy()
  assert r_model.map_manager().map_data()[0]==new_r_model.map_manager().map_data()[0]

  # Make a customized_copy
  new_r_model=r_model.customized_copy(model=r_model.model())
  assert new_r_model.model() is r_model.model()
  assert not new_r_model.map_dict() is r_model.map_dict()

  new_r_model=r_model.customized_copy(model=r_model.model(),map_dict=r_model.map_dict())
  assert new_r_model.model() is r_model.model()
  assert new_r_model.map_dict() is r_model.map_dict()
  print (r_model)

  # Initialize a map
  new_r_model.initialize_maps(map_value=6)
  assert new_r_model.map_manager().map_data()[225] == 6

  # Create a soft mask around model
  new_r_model.create_mask_around_atoms(mask_atoms_atom_radius=8,soft_mask=True)
  s = (new_r_model.get_map_manager('mask').map_data() > 0.5)
  assert approx_equal( (s.count(True),s.size()), (339,2048))

  new_r_model.create_mask_around_atoms(soft_mask=False, mask_atoms_atom_radius=8)
  s = (new_r_model.get_map_manager('mask').map_data() > 0.5)
  assert approx_equal( (s.count(True),s.size()), (35,2048))

  # Mask around edges 
  r_model=dc.deep_copy()
  r_model.create_mask_around_edges()
  s = (r_model.get_map_manager('mask').map_data() > 0.5)
  assert approx_equal( (s.count(True),s.size()), (1176,2048))

  # Mask around density
  r_model=dc.deep_copy()
  r_model.create_mask_around_density(soft_mask=False)
  s = (r_model.get_map_manager('mask').map_data() > 0.5)
  assert approx_equal( (s.count(True),s.size()), (856,2048))

  # Apply the mask to one map
  r_model.apply_mask_to_map('map_manager')
  s = (r_model.map_manager().map_data() > 0.)
  assert approx_equal( (s.count(True),s.size()), (424,2048))
  s = (r_model.map_manager().map_data() != 0.)
  assert approx_equal( (s.count(True),s.size()), (856,2048))
  assert approx_equal ((r_model.map_manager().map_data()[225]),-0.0418027862906)

  # Apply any mask to one map
  r_model.apply_mask_to_map('map_manager',mask_key='mask')
  s = (r_model.map_manager().map_data() > 0.)
  assert approx_equal( (s.count(True),s.size()), (424,2048))
  s = (r_model.map_manager().map_data() != 0.)
  assert approx_equal( (s.count(True),s.size()), (856,2048))
  assert approx_equal ((r_model.map_manager().map_data()[225]),-0.0418027862906)

  # Apply the mask to all maps
  r_model.apply_mask_to_maps()
  s = (r_model.map_manager().map_data() > 0.)
  assert approx_equal( (s.count(True),s.size()), (424,2048))
  s = (r_model.map_manager().map_data() != 0.)
  assert approx_equal( (s.count(True),s.size()), (856,2048))
  assert approx_equal ((r_model.map_manager().map_data()[225]),-0.0418027862906)

  # Apply the mask to all maps, setting outside value to mean inside
  r_model.apply_mask_to_maps(set_outside_to_mean_inside=True)
  s = (r_model.map_manager().map_data() > 0.)
  assert approx_equal( (s.count(True),s.size()), (424,2048))
  s = (r_model.map_manager().map_data() != 0.)
  assert approx_equal( (s.count(True),s.size()), (2048,2048))
  assert approx_equal ((r_model.map_manager().map_data()[2047]),-0.0759598612785)
  s = (r_model.get_map_manager('mask').map_data() >  0).as_1d()
  inside = r_model.map_manager().map_data().as_1d().select(s)
  outside = r_model.map_manager().map_data().as_1d().select(~s)
  assert approx_equal ((inside.min_max_mean().max,outside.min_max_mean().max),
   (0.317014873028,-0.0159585822888))




  print ("OK")

if __name__ == "__main__":
  args = sys.argv[1:]
  if not args:
    import libtbx.load_env
    file_name = libtbx.env.under_dist(
      module_name = "iotbx",
      path = "ccp4_map/tst_input.map")
    args = [file_name]
  exercise(file_name = args[0])



  print ("OK")

if __name__ == "__main__":
  args = sys.argv[1:]
  if not args:
    import libtbx.load_env
    file_name = libtbx.env.under_dist(
      module_name = "iotbx",
      path = "ccp4_map/tst_input.map")
    args = [file_name]
  exercise(file_name = args[0])
