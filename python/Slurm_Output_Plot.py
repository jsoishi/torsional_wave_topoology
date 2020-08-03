import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import os

root=os.path.join('Z:\\','torsional_wave_topoology','python')
slurmouts=['slurm-6904.out','slurm-6913.out']
files=[]
legends=['Nmax63, t=1', 'Nmax47, t=2']

#read files
for i in range(len(slurmouts)):
    slurmouts[i]=os.path.join(root, slurmouts[i])
    files.append(open(slurmouts[i]))

#read values
E0=[]
t=[]
dt=[]
counter=0

for file in files:
    E0.append([])
    dt.append([])
    t.append([])
    for lines in file:
        if 'iter' in lines:
            i = lines.find("dt=")
            j = lines.find(" t=")
            k = lines.find("E0=")
            dt[counter].append(float(lines[3+i:15+i]))
            t[counter].append(float(lines[3+j:15+j]))
            E0[counter].append(float(lines[3+k:15+k]))
    counter+=1
    
    
plt.figure(figsize=[16,9])
for i in range(counter):
    plt.plot(t[i],E0[i],label=legends[i])
plt.xlabel("diffusion time")
plt.ylabel("E0")
plt.title("Ek5e-6Ra900, Lmax191, N63vs47")
plt.legend()
plt.savefig('E0 plot',dpi=400)