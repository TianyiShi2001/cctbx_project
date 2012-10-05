#include <mmtbx/error.h>
#include <cctbx/geometry_restraints/bond.h>
#include <cctbx/geometry/geometry.h>

#include <cmath>
#include <set>
#include <iostream>

namespace mmtbx { namespace geometry_restraints {
  namespace af = scitbx::af;

  struct reference_coordinate_proxy
  {
    //! Support for shared_proxy_select.
    typedef af::tiny<unsigned, 1> i_seqs_type;

    // default initializer
    reference_coordinate_proxy () {}

    reference_coordinate_proxy(
      i_seqs_type const& i_seqs_,
      scitbx::vec3<double> ref_sites_,
      double weight_)
    :
      i_seqs(i_seqs_),
      ref_sites(ref_sites_),
      weight(weight_)
    {}

    // Support for proxy_select (and similar operations)
    reference_coordinate_proxy(
      i_seqs_type const& i_seqs_,
      reference_coordinate_proxy const& proxy)
    :
      i_seqs(i_seqs_),
      ref_sites(proxy.ref_sites),
      weight(proxy.weight)
    {}

    i_seqs_type i_seqs;
    scitbx::vec3<double> ref_sites;
    double weight;
  };

  inline
  double
  reference_coordinate_residual_sum(
    af::const_ref<scitbx::vec3<double> > const& sites_cart,
    af::const_ref<reference_coordinate_proxy> const& proxies,
    af::ref<scitbx::vec3<double> > const& gradient_array)
  {
    double residual_sum = 0, weight;
    scitbx::vec3<double> site, ref_site, delta;
    scitbx::vec3<double> gradient;
    for (std::size_t i = 0; i < proxies.size(); i++) {
      reference_coordinate_proxy proxy = proxies[i];
      af::tiny<unsigned, 1> const& i_seqs = proxy.i_seqs;
      MMTBX_ASSERT(i_seqs[0] < sites_cart.size());
      site = sites_cart[ i_seqs[0] ];
      ref_site = proxy.ref_sites;
      weight = proxy.weight;
      delta[0] = site[0] - ref_site[0];
      delta[1] = site[1] - ref_site[1];
      delta[2] = site[2] - ref_site[2];
      residual_sum += ( (delta[0]*delta[0]*weight)+
                        (delta[1]*delta[1]*weight)+
                        (delta[2]*delta[2]*weight) );
      gradient[0] = delta[0]*2.0*weight;
      gradient[1] = delta[1]*2.0*weight;
      gradient[2] = delta[2]*2.0*weight;
      gradient_array[ i_seqs[0] ] += gradient;
    }
    return residual_sum;
  }
}}
