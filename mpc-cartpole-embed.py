#%% 
import torch, time, sys
import autograd
import autograd.numpy as np
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import scipy.integrate
solve_ivp = scipy.integrate.solve_ivp

EXPERIMENT_DIR = './experiment-cartpole-embed/'
sys.path.append(EXPERIMENT_DIR)

from data import get_dataset, get_trajectory, dynamics_fn, hamiltonian_fn, arrange_data, get_field
from nn_models import MLP, PSD, DampMatrix
from hnn import HNN, HNN_structure, HNN_structure_cart_embed
from utils import L2_loss, from_pickle

from torchdiffeq import odeint 

#%%
DPI = 300
FORMAT = 'png'
LINE_SEGMENTS = 10
ARROW_SCALE = 40
ARROW_WIDTH = 6e-3
LINE_WIDTH = 2

def get_args():
    return {'num_angle': 1,
         'hidden_dim': 800,
         'learn_rate': 1e-3,
         'nonlinearity': 'tanh',
         'total_steps': 2000,
         'print_every': 200,
         'name': 'pend',
         'gridsize': 10,
         'input_noise': 0.5,
         'seed': 0,
         'save_dir': './{}'.format(EXPERIMENT_DIR),
         'fig_dir': './figures',
         'num_points': 3,
         'gpu': 2}

class ObjectView(object):
    def __init__(self, d): self.__dict__ = d

args = ObjectView(get_args())

#%%
# device = torch.device('cuda:' + str(args.gpu) if torch.cuda.is_available() else 'cpu')
device = torch.device('cpu')
def get_model(args, baseline, structure, naive, damping, num_points):
    M_net = PSD(3, args.hidden_dim, 2).to(device)
    g_net = MLP(3, args.hidden_dim, 2).to(device)
    if structure == False:
        if naive and baseline:
            raise RuntimeError('argument *baseline* and *naive* cannot both be true')
        elif naive:
            input_dim = 6
            output_dim = 5
        elif baseline:
            input_dim = 6
            output_dim = 4
        else:
            input_dim = 5
            output_dim = 1
        nn_model = MLP(input_dim, args.hidden_dim, output_dim, args.nonlinearity).to(device)
        model = HNN_structure_cart_embed(args.num_angle, H_net=nn_model, M_net=M_net, g_net=g_net, device=device, baseline=baseline, naive=
        naive)
    elif structure == True and baseline ==False and naive==False:
        V_net = MLP(3, 800, 1).to(device)
        model = HNN_structure_cart_embed(args.num_angle, M_net=M_net, V_net=V_net, g_net=g_net, device=device, baseline=baseline, structure=True).to(device)
    else:
        raise RuntimeError('argument *structure* is set to true, no *baseline* or *naive*!')

    if naive:
        label = '-naive_ode'
    elif baseline:
        label = '-baseline_ode'
    else:
        label = '-hnn_ode'
    struct = '-struct' if structure else ''
    path = '{}/{}{}{}-p{}.tar'.format(args.save_dir, args.name, label, struct, args.num_points)
    model.load_state_dict(torch.load(path, map_location=device))
    return model

# naive_ode_model = get_model(args, baseline=False, structure=False, naive=True, damping=False, num_points=args.num_points)
base_ode_model = get_model(args, baseline=True, structure=False, naive=False, damping=False, num_points=args.num_points)
hnn_ode_model = get_model(args, baseline=False, structure=False, naive=False, damping=False, num_points=args.num_points)
hnn_ode_struct_model = get_model(args, baseline=False, structure=True, naive=False, damping=False, num_points=args.num_points)


#%%
from mpc import mpc
from mpc.mpc import QuadCost, GradMethods
from tqdm import tqdm

n_batch, n_state, n_ctrl, mpc_T, T = 1, 5, 1, 20, 250
u_lower = -10.0 * torch.ones(mpc_T, n_batch, n_ctrl, dtype=torch.float32, device=device)
u_upper = 10.0 * torch.ones(mpc_T, n_batch, n_ctrl, dtype=torch.float32, device=device)
u_init = None #1.0 * torch.ones(T, n_batch, n_ctrl, dtype=torch.float32, device=device, requires_grad=True)

x0 = 0.0 ; q0 = 0.2
x_init = torch.tensor([[x0, np.cos(q0), np.sin(q0), 0, 0]], dtype=torch.float32, device=device, requires_grad=True).view(n_batch, n_state)
x = x_init
# cost
C = torch.diag(torch.tensor([0.0, 1.0, 100.0, 1.0, 1.0, 0.1], dtype=torch.float32, device=device)).view(1, 1, 6, 6)
C = C.repeat(mpc_T, n_batch, 1, 1) 
c = torch.tensor([0.0, -1.0, 0.0, 0.0, 0.0, 0.0], dtype=torch.float32, device=device).view(1, 1, 6)
c = c.repeat(mpc_T, n_batch, 1)

class Discrete_wrapper(torch.nn.Module):
    def __init__(self, diff_model):
        super(Discrete_wrapper, self).__init__()
        self.diff_model = diff_model

    def forward(self, x, u): 
        # y0_u = torch.cat((x, u), dim = 1)
        with torch.enable_grad():
            if len(x.shape) == 1:
                x = torch.unsqueeze(x, dim=0)
            if len(u.shape) == 1:
                u = torch.unsqueeze(u, dim=0)
            n_b = x.shape[0]
            y0_u = torch.cat((x, u), dim=1)
            y_u_new = odeint(self.diff_model, y0_u, torch.linspace(0.0, 0.02, 2), method='rk4')
            y_u_new = y_u_new[-1,:,:]
            y_new, u_new = torch.split(y_u_new, [5, 1], dim=1)
        return y_new

discrete_hnn_struct = Discrete_wrapper(hnn_ode_struct_model)
discrete_hnn = Discrete_wrapper(hnn_ode_model)
discrete_base = Discrete_wrapper(base_ode_model)



#%%
import gym 
import myenv
env = gym.make('MyCartPole-v0')
env.reset()
env.state = np.array([x0, 0.0, q0, 0.0], dtype=np.float32)
x = env._get_obs()

print('Initial condition: {}'.format(x))
#%%
actual_action = []
actual_states = []
actual_states.append(x)
for t in tqdm(range(T)):
    tensor_x = torch.tensor(x, dtype=torch.float32, device=device).view(1, 5)
    # tensor_x = x
    nominal_states, nominal_actions, nominal_objs = mpc.MPC(
        n_state=n_state, 
        n_ctrl=n_ctrl, 
        T=mpc_T,
        u_init=u_init,
        u_lower=u_lower,
        u_upper=u_upper,
        lqr_iter=50,
        verbose=1,
        n_batch=n_batch,
        exit_unconverged=False,
        detach_unconverged=False,
        grad_method=GradMethods.AUTO_DIFF,
        linesearch_decay=0.2, 
        max_linesearch_iter=5,
        eps=1e-2,
    )(tensor_x, QuadCost(C, c), discrete_hnn_struct)

    u_init = torch.cat((nominal_actions[1:], torch.zeros(1, n_batch, n_ctrl, device=device)), dim=0)
    next_action = nominal_actions[0].detach().cpu().numpy()
    print(next_action)
    # x = discrete_hnn_struct(x, nominal_actions[0])
    x, _, _, _ = env.step(next_action[0])

    actual_action.append(next_action[0])
    actual_states.append(x)


#%%
actual_action = np.stack(actual_action)
actual_states = np.stack(actual_states)
#%%
fig = plt.figure(figsize=[10, 10], dpi=DPI)
plt.subplot(7, 1, 1)
plt.plot(actual_states[:, 0])
plt.ylabel("$x$", fontsize=14)

plt.subplot(7, 1, 2)
plt.plot(actual_states[:, 1])
plt.ylabel("$cos(q)$", fontsize=14)

plt.subplot(7, 1, 3)
plt.plot(actual_states[:, 2])
plt.ylabel("$sin(q)$", fontsize=14)

plt.subplot(7, 1, 4)
plt.plot(actual_states[:, 3])
plt.ylabel("$\dot{x}$", fontsize=14)

plt.subplot(7, 1, 5)
plt.plot(actual_states[:, 4])
plt.ylabel("$\dot{q}$", fontsize=14)

plt.subplot(7, 1, 6)
plt.plot(actual_states[:, 0])
plt.ylabel("$u$", fontsize=14)
fig.savefig('{}/cartpole-mpc-p{}.{}'.format(args.fig_dir, args.num_points, FORMAT))

#%%
