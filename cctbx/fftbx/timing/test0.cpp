#include <iostream>
#include <vector>
#include <cctbx/fftbx/complex_to_complex_3d.h>
#include <cctbx/fftbx/real_to_complex_3d.h>

int main(void)
{
  std::size_t i;

  cctbx::fftbx::complex_to_complex<double> cfft(10);
  std::vector<std::complex<double> > vc(cfft.N());
  for(i=0;i<cfft.N();i++) {
    vc[i] = std::complex<double>(2.*i, 2.*i+1.);
  }
  cfft.forward(vc.begin());
  for(i=0;i<cfft.N();i++) {
    std::cout << vc[i].real() << " " << vc[i].imag() << std::endl;
  }
  cfft.backward(vc.begin());
  for(i=0;i<cfft.N();i++) {
    std::cout << vc[i].real() << " " << vc[i].imag() << std::endl;
  }

  cctbx::fftbx::real_to_complex<double> rfft(10);
  std::vector<double> vr(2 * rfft.Ncomplex());
  for(i=0;i<rfft.Nreal();i++) {
    vr[i] = 1.*i;
  }
  rfft.forward(vr.begin());
  for(i=0;i<2*rfft.Ncomplex();i++) {
    std::cout << vr[i] << std::endl;
  }
  rfft.backward(vr.begin());
  for(i=0;i<rfft.Nreal();i++) {
    std::cout << vr[i] << std::endl;
  }

  cctbx::fftbx::complex_to_complex_3d<double> cfft3d(2, 3, 5);
  cctbx::dimension<3> dim_c3d(cfft3d.N());
  std::vector<std::complex<double> > vc3d(dim_c3d.size1d());
  cctbx::vecrefnd<std::complex<double>, cctbx::dimension<3> >
  c3dmap(vc3d.begin(), dim_c3d);
  cfft3d.forward(c3dmap);
  cfft3d.backward(c3dmap);

  cctbx::fftbx::real_to_complex_3d<double> rfft3d(3, 4, 5);
  cctbx::dimension<3> dim_r3d(rfft3d.Mreal());
  std::vector<double> vr3d(dim_r3d.size1d());
  cctbx::vecrefnd<double, cctbx::dimension<3> >
  r3dmap(vr3d.begin(), dim_r3d);
  rfft3d.forward(r3dmap);
  rfft3d.backward(r3dmap);
#ifdef NEVER_DEFINED
#endif

  return 0;
}
