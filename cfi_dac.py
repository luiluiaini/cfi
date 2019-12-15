#!/usr/bin/env python
#
# Correlation Function Inversion
#
# find pairs using divide and conquer
# 
# Estimating the mesospheric neutral wind correlation function with different spatial and temporal lags
# using meteor radar measurements.
#
# Juha Vierinen, 2018
#
import numpy as n
import h5py
import matplotlib.pyplot as plt
import itertools
import scipy.misc as sm
import scipy.signal as ss
import scipy.interpolate as si
import scipy.optimize as sio

n.set_printoptions(precision=3)
# for parallel processing.
from mpi4py import MPI
comm = MPI.COMM_WORLD

# constants,
# TBD add altitude and latitude dependence
# (won't make a huge difference, but just to be complete).
latdeg2km=111.321
londeg2km=65.122785

sf_names=["uu","vv","ww","uv","uw","vw"]
# end constants

def vel(t,alt,lats,lons,times,rgs,v,dt,dh):
    '''
     evaluate mean wind without gradients. 
     tbd: implement better interpolation and linear gradients for mean wind.    
    '''
    ti=n.array(n.round((t-times[0])/dt),dtype=n.int)
    hi=n.array(n.floor((alt-rgs[0])/dh),dtype=n.int)
    hi[hi>(v.shape[2]-1)]=v.shape[2]-1
    ti[ti<0]=0
    ti[ti>(v.shape[1]-1)]=(v.shape[1]-1)
    return(v[0,ti,hi],v[1,ti,hi])


def get_meas(meas_file="res/simone_nov2018_multilink_juha_30min_1000m.h5",
             mean_rem=False,
             plot_dops=False,
             dcos_thresh=0.8,
             mean_wind_file="res/mean_wind_4h.h5"):
    '''
    read measurements, subtract mean wind if requested.
    the mean wind is whatever is in the file
    '''
    h=h5py.File(meas_file,"r")
    
    t=n.copy(h["t"].value)
    lats=n.copy(h["lats"].value)
    lons=n.copy(h["lons"].value)
    heights=n.copy(h["heights"].value)
    braggs=n.copy(h["braggs"].value)
    dops=n.copy(h["dops"].value)
    if "dcos" in h.keys():
        dcoss=n.copy(h["dcos"].value)
    else:
        dcoss=n.zeros([len(t),2])

    # dcos thresh
    dcos2=n.sqrt(dcoss[:,0]**2.0+dcoss[:,1]**2.0)
    ok_idx=n.where(dcos2 < dcos_thresh)[0]
    
    t=t[ok_idx]
    lats=lats[ok_idx]
    lons=lons[ok_idx]
    heights=heights[ok_idx]
    braggs=braggs[ok_idx,:]
    dops=dops[ok_idx]
    dcoss=dcoss[ok_idx,:]

    # remove mean wind (high-pass filter)
    if mean_rem:
        hm=h5py.File(mean_wind_file,"r")
        # grid
        times=n.copy(hm["times"].value)
        rgs=n.copy(hm["rgs"].value)
        v=n.copy(hm["v"].value)
        dt=n.copy(hm["dt"].value)
        print(dt)
        dh=n.copy(hm["dh"].value)
        mlat0=n.copy(hm["lat0"].value)
        mlon0=n.copy(hm["lon0"].value)
        hm.close()

        # interpolate zonal and merid wind
        vu,vv=vel(t,heights,lats,lons,times,rgs,v,dt,dh)
        
        # residual vel after removing mean horizontal wind
        dopsp = dops + (vu*braggs[:,0]+vv*braggs[:,1])/2.0/n.pi
        stdev_est=n.nanmedian(n.abs(n.nanmean(dopsp)-dopsp))
        stdev_est2=n.nanmedian(n.abs(n.nanmean(dops)-dops))
        
        if plot_dops:
            plt.plot(dops,".",label="orig")
            plt.plot(dopsp,".",label="hp")
            print(stdev_est)
            print(stdev_est2)
            plt.axhline(5*stdev_est)
            plt.axhline(-5*stdev_est)    
            plt.legend()
            plt.show()
        resid=n.abs(n.nanmean(dopsp)-dopsp)
        ok_idx=n.where(n.isfinite(dopsp) & (resid < 5*stdev_est) )[0]
        
        t=t[ok_idx]
        lats=lats[ok_idx]
        lons=lons[ok_idx]
        heights=heights[ok_idx]
        braggs=braggs[ok_idx,:]
        dops=dopsp[ok_idx]
        dcoss=dcoss[ok_idx,:]
    h.close()
    return({"t":t,"lats":lats,"lons":lons,"heights":heights,"braggs":braggs,"dops":dops,"dcoss":dcoss})
    

def cfi(m,
        h0=90,      # The height that we want (km) 
        dh=2,       # delta height (how much the height can differ from h0)
        ds_z=1.0,   # delta s_z how much the vertical lag can differ from one another
                    # vertical lag is s_z +/- ds_z/2
        s_z=0.0,    # vertical component of the spatial lag (s_z)
        s_x=0.0,    # east-west component of the spatial lag
        ds_x=1.0,   # delta s_x lag-resolution
        s_y=0.0,    # north-south component of the spatial lag
        ds_y=0.0,   # lag resolution 
        s_h=0.0,    # horizontal distance of the spatial lag (used instead of s_x and s_y if horizontal_dist=True)
        ds_h=100.0, # lag-resolution
        tau=0.0,    # temporal lag 
        dtau=300.0, # temporal lag resolution (lag can be tau +/- dtau/2)
        horizontal_dist=False, # do we use spatial lag specified using horizontal distance
                               # if true, then s_h specifies the horizontal lag, otherwise
                               # s_x and s_y specify it.
        hour_of_day=0.0,    # hour of day
        dhour_of_day=48.0,  # length of a time bin
        plot_thist=False):
    '''
    Calculate various lags. Use tree-like sorting of measurements to reduce the time
    to find pairs of measurements (not 100% tested).
    '''

    # this is where we read measurements from the measurement object.
    t=m["t"]
    heights=m["heights"]
    dops=m["dops"]
    lats=m["lats"]
    lons=m["lons"]
    braggs=m["braggs"]

    # Figure out hour of day (UTC)
    hod=n.mod(t/3600.0,24)
    # (idx_for_dimension_0, idx_for_dimension_1, ...)
    t_idx=n.where(n.abs(hod-hour_of_day)<=(dhour_of_day/2.0))[0]
    # select only the subset of measurements
    t=t[t_idx]
    heights=heights[t_idx]
    dops=dops[t_idx]
    lats=lats[t_idx]
    lons=lons[t_idx]
    braggs=braggs[t_idx,:]
    
    acf=n.zeros(6)
    err=n.zeros(6)
    
    pairs=[]
    pair_dict={}

    tods=[]    
    taus=[]
    s_xs=[]
    s_ys=[]
    s_zs=[]
    s_hs=[]                
    
    hor_dists=[]

    n_times=int((n.max(t)-n.min(t))/(dtau))
    t0=n.min(t)
    
    for i in range(n_times):
        it0=i*dtau+t0
        if i == (n_times-1):
            it1=n.max(t)
        else:
            it1=i*dtau+2*dtau+t0

        # filter heights
        idx0=n.where( (heights > (h0-dh*0.5)) & (heights < (h0+dh*0.5)) & (t>it0) &(t< it1) )[0]
        idx1=n.where( (heights > (h0+s_z-0.5*dh)) & (heights < (h0+s_z+0.5*dh)) & ( t > (it0+tau)) &  (t< (it1+tau)) )[0]

        if False:
            plt.plot(t[idx0],heights[idx0],"+")
            plt.plot(t[idx1],heights[idx1],"o")
            plt.xlim([n.min(t),n.max(t)])
            plt.show()
        
#        print("%d/%d s_z %1.2f h0 %1.2f h1 %1.2f tau %1.2f"%(i,n_times,s_z,n.mean(heights[idx0]),n.mean(heights[idx1]),tau))

        for ki,k in enumerate(idx0):
            lat0=lats[k]
            lon0=lons[k]
            mt0=t[k]
            hg0=heights[k]        

            if horizontal_dist:
                dist_filter = (n.abs(n.sqrt( (latdeg2km*(lats[idx1]-lat0))**2.0 + (londeg2km*(lons[idx1]-lon0))**2.0 )-s_h) < ds_h/2.0)
            else:
                dist_filter = (n.abs((latdeg2km*(lats[idx1]-lat0))-s_y) < ds_y/2.0 ) & ( n.abs((londeg2km*(lons[idx1]-lon0))-s_x) < ds_x/2.0 )

            idxt=idx1[n.where( dist_filter &
                               ( n.abs( (heights[idx1]-hg0) - s_z ) < (ds_z/2.0) ) &
                               ( idx1 != k ) &
                               ( n.abs( t[idx1] - mt0 - tau ) < dtau/2.0 ) )[0]]
            for l in idxt:
                if "%d-%d"%(k,l) not in pair_dict:
                    hor_dist = n.sqrt( (latdeg2km*(lats[l]-lat0))**2.0+(londeg2km*(lons[l]-lon0))**2.0)
                    # filter our measurements that are too close
                    if hor_dist > 2.0 and n.abs(t[l]-t[k])>2.0:
                        pair_dict["%d-%d"%(k,l)]=True
                        pair_dict["%d-%d"%(l,k)]=True
                        pairs.append((k,l))
                        tods.append(t[k])
                        taus.append(t[l]-t[k])
                        s_zs.append(heights[l]-heights[k])
                        s_xs.append(londeg2km*(lons[l]-lon0))
                        s_ys.append(latdeg2km*(lats[l]-lat0))
                        s_hs.append(hor_dist)


    # histogram and interpolate the number of measurements as a function of day
    tods=n.array(tods)
    # 30 minute bins, 0..24 hours utc histogram 
    thist,tbins=n.histogram(n.mod(tods/3600.0,24),bins=48)
    
    if plot_thist:
        plt.plot(tbins[0:len(thist)],thist)
        plt.show()
    # make sure that weight is at least 1.0 (one measurement per hour)
    thist[thist < 1.0]=1.0
    tbins2=0.5*(tbins[0:(len(tbins)-1)]+tbins[1:(len(tbins))])
    # start with 0 hours
    tbins2[0]=0.0
    # end with 24 hours
    tbins2[len(tbins2)-1]=24.0
    countf=si.interp1d(tbins2,thist)
                        
    n_meas=len(pairs)

    A=n.zeros([n_meas,6])
    Ao=n.zeros([n_meas,6])
    
    m=n.zeros(n_meas)
    mo=n.zeros(n_meas)
    
    print("n_meas %d"%(n_meas))
    
    ws=[]
    
    for pi in range(n_meas):
        k=pairs[pi][0]
        l=pairs[pi][1]

        w=1.0/countf(n.mod( (t[k]-t0)/3600,24.0 ))
        ws.append(w)
        A[pi,0]=w*braggs[k,0]*braggs[l,0] # ku1*ku2
        Ao[pi,0]=braggs[k,0]*braggs[l,0] # ku1*ku2
            
        A[pi,1]=w*braggs[k,1]*braggs[l,1] # kv1*kv2
        Ao[pi,1]=braggs[k,1]*braggs[l,1] # kv1*kv2
            
        A[pi,2]=w*braggs[k,2]*braggs[l,2] # kw1*kw2
        Ao[pi,2]=braggs[k,2]*braggs[l,2] # kw1*kw2
            
        A[pi,3]=w*(braggs[k,0]*braggs[l,1]+braggs[k,1]*braggs[l,0]) # ku1*kv2 + kv1*ku2
        Ao[pi,3]=braggs[k,0]*braggs[l,1]+braggs[k,1]*braggs[l,0] # ku1*kv2 + kv1*ku2
            
        A[pi,4]=w*(braggs[k,0]*braggs[l,2]+braggs[k,2]*braggs[l,0]) # ku1*kw2 + kw1*ku2
        Ao[pi,4]=braggs[k,0]*braggs[l,2]+braggs[k,2]*braggs[l,0] # ku1*kw2 + kw1*ku2
            
        A[pi,5]=w*(braggs[k,1]*braggs[l,2]+braggs[k,2]*braggs[l,1]) # kv1*kw2 + kw1*kv2
        Ao[pi,5]=braggs[k,1]*braggs[l,2]+braggs[k,2]*braggs[l,1] # kv1*kw2 + kw1*kv2            
        
        m[pi]=w*((2*n.pi)**2.0)*dops[k]*dops[l]
        mo[pi]=((2*n.pi)**2.0)*dops[k]*dops[l]        
    try:
        ws=n.array(w)
        xhat=n.linalg.lstsq(A,m)[0]
        
        # inverse scale weights
        resid=(mo-n.dot(Ao,xhat))
        stdev=3.0*n.sqrt(n.mean(n.abs(resid)**2.0))
        # assuming all measurements are independent
        sigma=n.sqrt(n.diag(n.linalg.inv(n.dot(n.transpose(Ao),Ao))))*stdev
        acf[:]=xhat
        err[:]=sigma

    except:
        acf[:]=n.nan
        err[:]=n.nan

    if False:
        plt.hist(taus)
        plt.show()
        plt.hist(s_xs)
        plt.show()
        plt.hist(s_ys)
        plt.show()
        plt.hist(s_zs)
        plt.show()
    return(acf, err, n.mean(taus), n.mean(s_xs), n.mean(s_ys), n.mean(s_zs), n.mean(s_hs))


def hor_acfs(meas,h0=90,dh=2,tau=0.0,s_h=n.arange(0,400.0,25.0), ds_h=25.0, ds_z=1.0, dtau=900 ):
    '''
     Horizontal distance spatial correlation function
    '''
    n_lags=len(s_h)
    acfs=n.zeros([n_lags,6])
    errs=n.zeros([n_lags,6])

    names=["$G_{uu}$","$G_{vv}$","$G_{ww}$",
           "$G_{uv}$","$G_{uw}$","$G_{vw}$"]

    shs=[]
    for li in range(n_lags):
        acf,err,tau,sx,sy,sz,sh= cfi(meas, h0=h0, dh=dh, s_z=0.0, s_h=s_h[li], ds_h=ds_h, ds_z=ds_z, tau=tau,dtau=dtau, horizontal_dist=True)
        shs.append(sh)
        print("s_h %1.2f"%(sh))
        print(acf)
        acfs[li,:]=acf
        errs[li,:]=err
    shs=n.array(shs)


    ho=h5py.File("hor_acf_h_%1.2f_dtau_%1.2f_dsh_%1.2f.h5"%(h0,dtau,ds_h),"w")
    ho["h0"]=h0
    ho["dtau"]=dtau
    ho["ds_h"]=ds_h
    ho["acfs"]=acfs
    ho["errs"]=errs
    ho["shs"]=shs
    ho["sho"]=s_h
    ho.close()
    
 #   plt.subplot(121)
    for i in range(6):
        plt.plot(shs,acfs[:,i],label=names[i])

    plt.legend()
    plt.xlabel("Horizontal lag (km)")    
    plt.ylabel("Correlation (m$^2$/s$^2$)")
    plt.title("Horizontal ACF $\Delta s_z=%1.1f$ km, $\Delta \\tau = %1.1f$ s $\Delta s_h=%1.1f$ km"%(ds_z,dtau,ds_h))

    if False:
        plt.subplot(122)
        # todo: fit exponential to acf, to obtain zero-lag estimate
        sfu=2.0*1.15*acfs[0,0]-2.0*acfs[:,0]
        sfv=2.0*1.15*acfs[0,1]-2.0*acfs[:,1]
        plt.loglog(shs,sfu,"o-",label="$S_{uu}$")
        plt.loglog(shs,sfv,"o-",label="$S_{vv}$")
        a=sfu[2]/shs[2]**(2.0/3.0)
        plt.loglog(shs,a*shs**(2.0/3.0),label="$s^{2/3}$")
        plt.legend()
        plt.xlabel("Horizontal lag (km)")    
        plt.ylabel("Structure function (m$^2$/s$^2$)")
        plt.title("Horizontal structure function")
    plt.show()



def ver_acfs(meas,h0=90,dh=1,tau=0.0,s_z=n.arange(-10,10.0,1.0), s_h=0.0, ds_h=50.0, ds_z=1.0, dtau=900 ):
    '''
     Vertical lag spatial correlation function
    '''

    n_lags=len(s_z)
    acfs=n.zeros([n_lags,6])
    errs=n.zeros([n_lags,6])

    names=["$G_{uu}$","$G_{vv}$","$G_{ww}$",
           "$G_{uv}$","$G_{uw}$","$G_{vw}$"]

    szs=[]
    for li in range(n_lags):
        print("s_z %1.1f"%(s_z[li]))
        acf,err,tau,sx,sy,sz,sh= cfi(meas, h0=h0, dh=dh, s_z=s_z[li], s_h=0.0, ds_h=ds_h, ds_z=ds_z, tau=tau, dtau=dtau, horizontal_dist=True)
        szs.append(sz)
        print("s_z %1.2f"%(sz))
        print(acf)
        acfs[li,:]=acf
        errs[li,:]=err
    szs=n.array(szs)




    
    plt.subplot(121)
    for i in range(6):
        plt.plot(szs,acfs[:,i],label=names[i])

    plt.legend()
    plt.xlabel("Vertical lag (km)")    
    plt.ylabel("Correlation (m$^2$/s$^2$)")
    plt.title("Vertical ACF $\Delta s_z=%1.1f$, $\Delta \\tau = %1.1f$ s"%(ds_z,dtau))

    plt.subplot(122)


    
    # estimate structure function
    # tbd, estimate zero lag with exp function
    sfu=2.0*1.01*acfs[0,0]-2.0*acfs[:,0]
    sfv=2.0*1.01*acfs[0,1]-2.0*acfs[:,1]
    # don't show zero-lag. it doesn't make sense.
    plt.loglog(szs,sfu,"o-",label="$S'_{uu}$")
    plt.loglog(szs,sfv,"o-",label="$S'_{vv}$")
    a=sfu[2]/szs[2]**(2.0/3.0)
#    shs[0]=0.0
    plt.loglog(szs,a*szs**(2.0/3.0),label="$s^{2/3}$")
    plt.legend()
    plt.xlabel("Vertical lag (km)")    
    plt.ylabel("Structure function (m$^2$/s$^2$)")
    plt.title("Vertical structure function")
    plt.show()



def temporal_acfs(meas,
                  h0=91,      # height
                  dh=1,       # width of height range
                  tau=n.arange(96)*900.0,
                  dtau=300.0, # temporal lag resolution
                  ds_h=25.0,  # horizontal lag resolution
                  ds_z=1.0):  # vertical lag resolution
    '''
     Temporal lag spatial correlation function
    '''
    n_lags=len(tau)
    acfs=n.zeros([n_lags,6])
    errs=n.zeros([n_lags,6])

    names=["$G_{uu}$","$G_{vv}$","$G_{ww}$",
           "$G_{uv}$","$G_{uw}$","$G_{vw}$"]

    for li in range(n_lags):
        
        print("tau %1.1f"%(tau[li]))
        
        acf,err,mtau,msx,msy,msz,msh = cfi(meas,
                                           h0=h0,
                                           dh=dh,
                                           s_z=0.0,
                                           s_h=0.0,
                                           ds_h=ds_h,
                                           ds_z=ds_z,
                                           tau=tau[li],
                                           dtau=dtau,
                                           horizontal_dist=True)
        
        print(acf)
        acfs[li,:]=acf
        errs[li,:]=err


    for i in range(6):
        plt.plot(tau,acfs[:,i],label=names[i])

    plt.legend()
    plt.xlabel("Temporal lag (s)")    
    plt.ylabel("Correlation (m$^2$/s$^2$)")
    plt.title("Temporal ACF")

    plt.show()

    ho=h5py.File("tacf_dtau_%1.0f_tau_%1.0f_ds_h_%1.2f.h5"%(dtau,n.max(tau),ds_h),"w")
    ho["tau"]=tau
    ho["acf"]=acfs
    ho["dtau"]=dtau
    ho["ds_h"]=ds_h
    ho.close()
    
    return(tau,acfs)
    

def example1a():
    # estimate a temporally high pass filtered horizontal acf
    meas=get_meas(mean_rem=True, plot_dops=False, mean_wind_file="res/mean_wind_4h.h5")
    hor_acfs(meas,h0=95.0,dh=2,ds_z=1.0,ds_h=25.0,s_h=n.arange(0,400.0,25.0),dtau=300.0)

def example1b():
    # estimate horizontal acf with no filtering
    meas=get_meas(mean_rem=False, plot_dops=False, mean_wind_file="res/mean_wind_4h.h5")
    hor_acfs(meas,h0=93.0,dh=1.0,ds_z=1.0,ds_h=25.0,s_h=n.arange(0,400.0,12.5),dtau=600.0)
    
def example2():    
    # estimate a high pass filtered vertical acf
    meas=get_meas(mean_rem=True,plot_dops=False, mean_wind_file="res/mean_wind_1h.h5")
    ver_acfs(meas,h0=89.0,dh=4.0,s_z=n.arange(0.0,10.0,1.0),dtau=200.0, tau=0.0, s_h=0.0, ds_h=50.0)

def example3():
    # estimate a temporal autocorrelation function
    meas=get_meas(mean_rem=False,plot_dops=False, mean_wind_file="res/mean_wind_4h.h5")

    dtau=900.0
    h_max=72.0
    n_t=int(h_max*3600.0/dtau)
    
    temporal_acfs(meas,h0=91.0,dh=4,ds_z=1.0,ds_h=50.0,dtau=dtau,tau=n.arange(float(n_t))*dtau)

def example4():
    # estimate a temporal autocorrelation function for high pass filtered measurements
    # at most 12 hours lag
    meas=get_meas(mean_rem=True,plot_dops=False, mean_wind_file="res/mean_wind_4h.h5")

    dtau=600.0
    h_max=12.0
    n_t=int(h_max*3600.0/dtau)
    
    temporal_acfs(meas,h0=93.5,dh=5,ds_z=1.0,ds_h=25.0,dtau=dtau,tau=n.arange(float(n_t))*dtau)

def example5():
    # full horizontal correlation function
    # 25 km resolution, 25 km lag spacing
    # 900 second lag resolution (+/- 450 seconds)
    meas=get_meas(mean_rem=False, plot_dops=False, mean_wind_file="res/mean_wind_4h.h5")
    hor_acfs(meas,h0=92.0,dh=5,ds_z=1.0,ds_h=25.0,s_h=n.arange(0,400.0,25.0),dtau=900.0)



def mean_wind_cf(heights=n.arange(80,111),
                 hour_of_day=n.arange(48)*0.5,
                 dtau=1800.0,   # half hour time resolution
                 ds_h=300.0):   # horizontal lag resolution
    # mean wind correlation functions for times of day at 30 minute time resolution
    # and 1 km height resolution
    #
    # read measurements. don't remove mean wind, as we are interested in the full correlation function
    meas=get_meas(mean_rem=False, plot_dops=False, mean_wind_file="res/mean_wind_4h.h5")
    
    n_heights=len(heights)
    n_hods=len(hour_of_day)
    
    # this is where we store the correlation functions
    C=n.zeros([n_hods,n_heights,6])

    for hi,h0 in enumerate(heights):
        for ti,t0 in enumerate(hour_of_day):
            print("doing height %1.2f hour of day %1.2f"%(h0,t0))
            acf,err,tau,sx,sy,sz,sh= cfi(meas,
                                         h0=h0, dh=2.0,  # only use measurements at height h0 +/- dh/2.0
                                         hour_of_day=t0, # only use measurements where utc hour of day is t0 +/- dhour_of_day/2
                                         dhour_of_day=1.0,   
                                         s_z=0.0, ds_z=2.0,
                                         s_h=0.0, ds_h=ds_h, 
                                         tau=0.0 ,dtau=1800.0,
                                         horizontal_dist=True)
            print(acf)
            C[ti,hi,:]=acf
            ho=h5py.File("mean_cf.h5","w")
            ho["C"]=C
            ho["heights"]=heights
            ho["hour_of_day"]=hour_of_day
            ho.close()
            
    ho=h5py.File("mean_cf.h5","w")
    ho["C"]=C
    ho["heights"]=heights
    ho["hour_of_day"]=hour_of_day
    ho.close()
    return(C)
    
    
#mean_wind_cf()
example1b()

#example2()
#example3()
#example4()
#example5()   
#example1()

