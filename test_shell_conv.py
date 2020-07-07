import numpy as np
import scipy.sparse      as sparse
import dedalus.public as de
from dedalus.core import arithmetic, timesteppers, problems, solvers
from dedalus.tools.parsing import split_equation
from dedalus.extras.flow_tools import GlobalArrayReducer
import dedalus_sphere
from mpi4py import MPI
import time

#me trying to use config files
#1-0 is for a day
import os
import sys
import configparser
from configparser import ConfigParser
from pathlib import Path

import matplotlib
import logging
logger = logging.getLogger(__name__)

matplotlib_logger = logging.getLogger('matplotlib')
matplotlib_logger.setLevel(logging.WARNING)

comm = MPI.COMM_WORLD
rank = comm.rank
size = comm.size

config_file = Path(sys.argv[-1])
config = ConfigParser()
config.read(str(config_file))

logger.info('Running with the following parameters:')
logger.info(config.items('parameters'))

params = config['parameters']

# create data dir using basename of cfg file
basedir = Path('frames')
outdir = "frames_" + config_file.stem
data_dir = basedir/outdir
logger.info(data_dir)
if rank == 0:
    if not data_dir.exists():
        data_dir.mkdir(parents=True)


Lmax = params.getint('Lmax')
Nmax = params.getint('Nmax')

# right now can't run with dealiasing
L_dealias = 1
N_dealias = 1

# parameters
Ekman = params.getfloat('Ekman')
Prandtl = 1
Rayleigh = params.getint('Rayleigh')
r_inner = 7/13
r_outer = 20/13
radii = (r_inner,r_outer)

# mesh must be 2D for plotting
mesh = [params.getint('Xn'),params.getint('Yn')]

c = de.coords.SphericalCoordinates('phi', 'theta', 'r')
d = de.distributor.Distributor((c,), mesh=mesh)
b    = de.basis.SphericalShellBasis(c, (2*(Lmax+1),Lmax+1,Nmax+1), radii=radii)
bk2  = de.basis.SphericalShellBasis(c, (2*(Lmax+1),Lmax+1,Nmax+1), k=2, radii=radii)
b_inner = b.S2_basis(radius=r_inner)
b_outer = b.S2_basis(radius=r_outer)
phi, theta, r = b.local_grids((1, 1, 1))
phig,thetag,rg= b.global_grids((1,1, 1))
theta_target = thetag[0,(Lmax+1)//2,0]

weight_theta = b.local_colatitude_weights(1)
weight_r = b.radial_basis.local_weights(1)*r**2

u = de.field.Field(dist=d, bases=(b,), tensorsig=(c,), dtype=np.complex128)
p = de.field.Field(dist=d, bases=(b,), dtype=np.complex128)
T = de.field.Field(dist=d, bases=(b,), dtype=np.complex128)
tau_u_inner = de.field.Field(dist=d, bases=(b_inner,), tensorsig=(c,), dtype=np.complex128)
tau_T_inner = de.field.Field(dist=d, bases=(b_inner,), dtype=np.complex128)
tau_u_outer = de.field.Field(dist=d, bases=(b_outer,), tensorsig=(c,), dtype=np.complex128)
tau_T_outer = de.field.Field(dist=d, bases=(b_outer,), dtype=np.complex128)

ez = de.field.Field(dist=d, bases=(b,), tensorsig=(c,), dtype=np.complex128)
ez['g'][1] = -np.sin(theta)
ez['g'][2] =  np.cos(theta)

r_vec = de.field.Field(dist=d, bases=(b,), tensorsig=(c,), dtype=np.complex128)
r_vec['g'][2] = r/r_outer

T_inner = de.field.Field(dist=d, bases=(b_inner,), dtype=np.complex128)
T_inner['g'] = 1.

# initial condition
A = 0.1
x = 2*r-r_inner-r_outer
T['g'] = r_inner*r_outer/r - r_inner + 210*A/np.sqrt(17920*np.pi)*(1-3*x**2+3*x**4-x**6)*np.sin(theta)**4*np.cos(4*phi)

# Parameters and operators
div = lambda A: de.operators.Divergence(A, index=0)
lap = lambda A: de.operators.Laplacian(A, c)
grad = lambda A: de.operators.Gradient(A, c)
dot = lambda A, B: arithmetic.DotProduct(A, B)
cross = lambda A, B: arithmetic.CrossProduct(A, B)
ddt = lambda A: de.operators.TimeDerivative(A)

# Problem
def eq_eval(eq_str):
    return [eval(expr) for expr in split_equation(eq_str)]
problem = problems.IVP([u, p, T, tau_u_inner, tau_T_inner, tau_u_outer, tau_T_outer])
problem.add_equation(eq_eval("Ekman*ddt(u) - Ekman*lap(u) + grad(p) = - Ekman*dot(u,grad(u)) + Rayleigh*r_vec*T - 2*cross(ez, u)"), condition = "ntheta != 0")
problem.add_equation(eq_eval("u = 0"), condition = "ntheta == 0")
problem.add_equation(eq_eval("div(u) = 0"), condition = "ntheta != 0")
problem.add_equation(eq_eval("p = 0"), condition = "ntheta == 0")
problem.add_equation(eq_eval("ddt(T) - lap(T)/Prandtl = - dot(u,grad(T))"))
problem.add_equation(eq_eval("u(r=7/13) = 0"), condition = "ntheta != 0")
problem.add_equation(eq_eval("tau_u_inner = 0"), condition = "ntheta == 0")
problem.add_equation(eq_eval("T(r=7/13) = T_inner"))
problem.add_equation(eq_eval("u(r=20/13) = 0"), condition = "ntheta != 0")
problem.add_equation(eq_eval("tau_u_outer = 0"), condition = "ntheta == 0")
problem.add_equation(eq_eval("T(r=20/13) = 0"))
logger.info("Problem built")

# Solver
solver = solvers.InitialValueSolver(problem, timesteppers.SBDF2)

# Add taus

# ChebyshevV
alpha_BC = (2-1/2, 2-1/2)

def C(N):
    ab = alpha_BC
    cd = (b.radial_basis.alpha[0]+2,b.radial_basis.alpha[1]+2)
    return dedalus_sphere.jacobi.coefficient_connection(N+1,ab,cd)

def BC_rows(N, num_comp):
    N_list = (np.arange(num_comp)+1)*(N + 1)
    return N_list

for subproblem in solver.subproblems:
    if subproblem.group[1] != 0:
        ell = subproblem.group[1]
        M = subproblem.M_min
        L = subproblem.L_min
        shape = M.shape
        subproblem.M_min[:,-8:] = 0
        subproblem.M_min.eliminate_zeros()
        N0, N1, N2, N3, N4 = BC_rows(Nmax, 5)
        tau_columns = np.zeros((shape[0], 8))
        tau_columns[  :N0,0] = (C(Nmax))[:,-1]
        tau_columns[N0:N1,1] = (C(Nmax))[:,-1]
        tau_columns[N1:N2,2] = (C(Nmax))[:,-1]
        tau_columns[N3:N4,3] = (C(Nmax))[:,-1]
        tau_columns[  :N0,4] = (C(Nmax))[:,-2]
        tau_columns[N0:N1,5] = (C(Nmax))[:,-2]
        tau_columns[N1:N2,6] = (C(Nmax))[:,-2]
        tau_columns[N3:N4,7] = (C(Nmax))[:,-2]
        subproblem.L_min[:,-8:] = tau_columns
        subproblem.L_min.eliminate_zeros()
        subproblem.expand_matrices(['M','L'])
    else: # ell = 0
        M = subproblem.M_min
        L = subproblem.L_min
        shape = M.shape
        subproblem.M_min[:,-8:] = 0
        subproblem.M_min.eliminate_zeros()
        N0, N1, N2, N3, N4 = BC_rows(Nmax, 5)
        subproblem.L_min[N3:N4,N4+3] = (C(Nmax))[:,-1].reshape((N0,1))
        subproblem.L_min[N3:N4,N4+7] = (C(Nmax))[:,-2].reshape((N0,1))
        subproblem.L_min.eliminate_zeros()
        subproblem.expand_matrices(['M','L'])

reducer = GlobalArrayReducer(d.comm_cart)

vol_test = np.sum(weight_r*weight_theta+0*p['g'])*np.pi/(Lmax+1)/L_dealias
vol_test = reducer.reduce_scalar(vol_test, MPI.SUM)
vol = 4*np.pi/3*(r_outer**3-r_inner**3)
vol_correction = vol/vol_test

t = 0.

t_list = []
E_list = []

report_cadence = 10

plot_cadence = 100 #original is 500
dpi = 150

plot = theta_target in theta

include_data = comm.gather(plot)

var = T['g']
name = 'T'
remove_m0 = True

if plot:
    i_theta = np.argmin(np.abs(theta[0,:,0] - theta_target))
    plot_data = var[:,i_theta,:].real.copy()
    plot_rec_buf = None
else:
    plot_data = np.zeros_like(var[:,0,:].real)

plot_rec_buf = None
if rank == 0:
    rec_shape = [size,] + list(var[:,0,:].shape)
    plot_rec_buf = np.empty(rec_shape,dtype=plot_data.dtype)

comm.Gather(plot_data, plot_rec_buf, root=0)

import matplotlib.pyplot as plt
def equator_plot(r, phi, data, index=None, pcm=None, cmap=None, title=None):
    if pcm is None:
        r_pad   = np.pad(r[0,0,:], ((0,1)), mode='constant', constant_values=(r_inner,r_outer))
        phi_pad = np.append(phi[:,0,0], 2*np.pi)
        fig, ax = plt.subplots(subplot_kw=dict(polar=True))
        r_plot, phi_plot = np.meshgrid(r_pad,phi_pad)
        pcm = ax.pcolormesh(phi_plot,r_plot,data, cmap=cmap)
        ax.set_rlim(bottom=0, top=r_outer)
        ax.set_rticks([])
        ax.set_aspect(1)

        pmin,pmax = pcm.get_clim()
        cNorm = matplotlib.colors.Normalize(vmin=pmin, vmax=pmax)
        ax_cb = fig.add_axes([0.8, 0.3, 0.03, 1-0.3*2])
        cb = fig.colorbar(pcm, cax=ax_cb, norm=cNorm, cmap=cmap)
        fig.subplots_adjust(left=0.05,right=0.85)
        if title is not None:
            ax_cb.set_title(title)
        pcm.ax_cb = ax_cb
        pcm.cb_cmap = cmap
        pcm.cb = cb
        return fig, pcm
    else:
        pcm.set_array(np.ravel(data))
        pcm.set_clim([np.min(data),np.max(data)])
        cNorm = matplotlib.colors.Normalize(vmin=np.min(data), vmax=np.max(data))
        pcm.cb.mappable.set_norm(cNorm)
        if title is not None:
            pcm.ax_cb.set_title(title)

if rank == 0:
    data = []
    for pd, id in zip(plot_rec_buf, include_data):
        if id: data.append(pd)
    data = np.array(data)
    data = np.transpose(data, axes=(1,0,2)).reshape((2*(Lmax+1),Nmax+1))
    if remove_m0:
        data -= np.mean(data, axis=0)
    fig, pcm = equator_plot(rg, phig, data, title=name+"'\n t = {:5.2f}".format(0), cmap = 'RdYlBu_r')
    plt.savefig( str(data_dir)+'/%s_%04i.png' %(name, solver.iteration//plot_cadence), dpi=dpi)

# timestepping loop
start_time = time.time()

#variable time step
safety = 0.1 # should work for SBDF2
threshold = 0.1
dr = np.gradient(r[0,0])

def calculate_dt(dt_old):
    local_freq  = np.abs(u['g'][2]/dr) + np.abs(u['g'][0]*(Lmax+1)) + np.abs(u['g'][1]*(Lmax+1))
    global_freq = reducer.global_max(local_freq)
    if global_freq == 0.:
        dt = np.inf
    else:
        dt = 1 / global_freq
        dt *= safety
    if dt > max_dt:
        dt = max_dt
    if dt < dt_old*(1+threshold) and dt > dt_old*(1-threshold):
        dt = dt_old
    return dt

# Integration parameters
dt = params.getfloat('dt')
max_dt = 5*dt

t_end = params.getfloat('t_end') #10 #1.25
solver.stop_sim_time = t_end

while solver.ok:

    dt=calculate_dt(dt)

    if solver.iteration % report_cadence == 0:
        E0 = np.sum(vol_correction*weight_r*weight_theta*u['g'].real**2)
        E0 = 0.5*E0*(np.pi)/(Lmax+1)/L_dealias/vol
        E0 = reducer.reduce_scalar(E0, MPI.SUM)
        T0 = np.sum(vol_correction*weight_r*weight_theta*T['g'].real**2)
        T0 = 0.5*T0*(np.pi)/(Lmax+1)/L_dealias/vol
        T0 = reducer.reduce_scalar(T0, MPI.SUM)
        logger.info("iter: {:d}, dt={:e}, t={:e}, E0={:e}, T0={:e}".format(solver.iteration, dt, solver.sim_time, E0, T0))
        t_list.append(solver.sim_time)
        E_list.append(E0)

    if solver.iteration % plot_cadence == 0:
        if plot:
            plot_data = var[:,i_theta,:].real.copy()

        comm.Gather(plot_data, plot_rec_buf, root=0)

        if rank == 0:
            data = []
            for pd, id in zip(plot_rec_buf, include_data):
                if id: data.append(pd)
            data = np.array(data)
            data = np.transpose(data, axes=(1,0,2)).reshape((2*(Lmax+1),Nmax+1))
            if remove_m0:
                data -= np.mean(data, axis=0)
            equator_plot(rg, phig, data, title=name+"'\n t = {:5.2f}".format(solver.sim_time), cmap='RdYlBu_r', pcm=pcm)
            fig.savefig(str(data_dir)+'/%s_%04i.png' %(name,solver.iteration//plot_cadence), dpi=dpi)

    solver.step(dt)

end_time = time.time()
if rank==0:
    print('simulation took: %f' %(end_time-start_time))
    t_list = np.array(t_list)
    E_list = np.array(E_list)
    np.savetxt(str(config_file.stem)+'_marti_conv.dat',np.array([t_list,E_list]))
