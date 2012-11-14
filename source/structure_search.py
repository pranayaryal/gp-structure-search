'''
Main file for performing structure search.

@authors: David Duvenaud (dkd23@cam.ac.uk)
          James Robert Lloyd (jrl44@cam.ac.uk)
          Roger Grosse (rgrosse@mit.edu)
          
Created November 2012          
'''

import flexiblekernel as fk
import grammar
import gpml
import utils.latex
import utils.fear
import config
from utils import gaussians, psd_matrices

import numpy as np
nax = np.newaxis
import pylab
import scipy.io
import sys
import os
import tempfile
import subprocess
import time

PRIOR_VAR = 100.


def load_mat(data_file, y_dim=1):
    '''Load a Matlab file'''
    data = scipy.io.loadmat(data_file)
    return data['X'], data['y'][:,y_dim-1], np.shape(data['X'])[1]

def proj_psd(H):
    assert np.allclose(H, H.T), 'not symmetric'
    d, Q = scipy.linalg.eigh(H)
    d = np.where(d > 0, d, 0.)
    return np.dot(Q, d[:, nax] * Q.T)
    

def laplace_approx(nll, opt_hyper, hessian, prior_var):
    d = opt_hyper.size
    
    hessian = proj_psd(hessian)

    # quadratic centered at opt_hyper with maximum -nll
    evidence = gaussians.Potential(np.zeros(d), psd_matrices.FullMatrix(hessian), -nll)
    evidence = evidence.translate(opt_hyper)

    # zero-centered Gaussian
    prior = gaussians.Potential.from_moments_iso(np.zeros(d), prior_var)

    # multiply the two Gaussians and integrate the result
    return -(evidence + prior).integral()


def expand_kernels(D, seed_kernels, verbose=False):    
    '''Makes a list of all expansions of a set of kernels.'''
      
    g = grammar.MultiDGrammar(D)
    print 'Seed kernels :'
    for k in seed_kernels:
        print k.pretty_print()
    kernels = []
    for k in seed_kernels:
        kernels = kernels + grammar.expand(k, g)
    kernels = grammar.remove_duplicates(kernels)
    print 'Expanded kernels :'
    for k in kernels:
        print k.pretty_print()
            
    return (kernels)

class ScoredKernel:
    def __init__(self, k_opt, nll, laplace_nle, bic_nle, noise):
        self.k_opt = k_opt
        self.nll = nll
        self.laplace_nle = laplace_nle
        self.bic_nle = bic_nle
        self.noise = noise
        
    def score(self, criterion='laplace'):
        return {'bic': self.bic_nle,
                'nll': self.nll,
                'laplace': self.laplace_nle
                }[criterion]
                
    @staticmethod
    def from_printed_outputs(nll, laplace, BIC, noise=None, kernel=None):
        return ScoredKernel(kernel, nll, laplace, BIC, noise)
    
    def __repr__(self):
        return 'ScoredKernel(k_opt=%s, nll=%f, laplace_nle=%f, bic_nle=%f, noise=%s' % \
            (self.k_opt, self.nll, self.laplace_nle, self.bic_nle, self.noise)
    
    @staticmethod
    def parse_results_string(line):
        v = locals().copy()
        v.update(fk.__dict__)
        v['nan'] = np.NaN;
        return eval(line, globals(), v)


def fear_experiment(data_file, results_filename, y_dim=1, subset=None, max_depth=2, k=2, \
                    verbose=True, sleep_time=60, n_sleep_timeout=20, re_submit_wait=60, \
                    description=''):
    '''Recursively search for the best kernel, in parallel on the fear cluster.'''

    X, y, D = load_mat(data_file, y_dim)
    
    current_kernels = list(fk.base_kernels(D))
        
    results = []              # All results.
    results_sequence = []     # Results sets indexed by level of expansion.
    for r in range(max_depth):   
        new_results = fear_run_experiments(current_kernels, X, y, verbose=verbose, \
                                           sleep_time=sleep_time, n_sleep_timeout=n_sleep_timeout, \
                                           re_submit_wait=re_submit_wait)
            
        results = results + new_results
        
        print
        results = sorted(results, key=ScoredKernel.score, reverse=True)
        for result in results:
            print result.nll, result.laplace_nle, result.bic_nle, result.k_opt.pretty_print()
        
        results_sequence.append(results)
        
        best_kernels = [r.k_opt for r in sorted(new_results, key=ScoredKernel.score)[0:k]]
        current_kernels = expand_kernels(D, best_kernels, verbose=verbose)

    # Write results to a file.  Todo: write a dictionary.
    results = sorted(results, key=ScoredKernel.score, reverse=True)
    with open(results_filename, 'w') as outfile:
        outfile.write('Experiment results for\n datafile = %s\n y_dim = %d\n subset = %s\n max_depth = %f\n k = %f\n Description = %s\n\n' % (data_file, y_dim, subset, max_depth, k, description)) 
        for (i, results) in enumerate(results_sequence):
            outfile.write('\n%%%%%%%%%% Level %d %%%%%%%%%%\n\n' % i)
            for result in results:
                outfile.write( result )      
            


def mkstemp_safe(directory, suffix):
    (os_file_handle, file_name) = tempfile.mkstemp(dir=directory, suffix=suffix)
    os.close(os_file_handle)
    return file_name


def qsub_matlab_code(code, verbose=True, local_dir ='../temp/', remote_dir ='./temp/', fear=None):
    # Write to a temp script
    script_file = mkstemp_safe(local_dir, '.m')
    shell_file = mkstemp_safe(local_dir, '.sh')
    
    f = open(script_file, 'w')
    f.write(code)
    f.close()
    
    # Local file reference without extension - MATLAB fails silently otherwise
    f = open(shell_file, 'w')
    f.write('/usr/local/apps/matlab/matlabR2011b/bin/matlab -nosplash -nojvm -nodisplay -singleCompThread -r ' \
            + script_file.split('/')[-1].split('.')[0] + '\n')
    f.close()
        
    utils.fear.copy_to(script_file, remote_dir + script_file.split('/')[-1], fear)
    utils.fear.copy_to(shell_file, remote_dir + shell_file.split('/')[-1], fear)
    
    job_id = utils.fear.qsub(shell_file)
    
    if verbose:
        print 'job id = %s' % job_id
    
    # Tell the caller where the script file was written
    return script_file, shell_file, job_id         


def fear_run_experiments(kernels, X, y, return_all=False, verbose=True, noise=None, iters=300, \
                         local_dir ='../temp/', remote_dir ='./temp/', \
                         sleep_time=10, n_sleep_timeout=6, re_submit_wait=60):
    '''Sends jobs to fear, waits for them, returns the results.'''
   
    # Make data into matrices in case they're unidimensional.
    if X.ndim == 1: X = X[:, nax]
    if y.ndim == 1: y = y[:, nax]
    data = {'X': X, 'y': y}
        
    if noise is None:
        noise = np.log(np.var(y)/10)   # Set default noise using a heuristic.
    
    fear = utils.fear.connect()
    
    # Submit all the jobs and remember where we put them
    data_files = []
    write_files = []
    script_files = []
    shell_files = []
    job_ids = []
    
    for kernel in kernels:
        
        # Create data file and results file
        data_files.append(mkstemp_safe(local_dir, '.mat'))
        write_files.append(mkstemp_safe(local_dir, '.mat'))
        
        scipy.io.savemat(data_files[-1], data)  # Save regression data
        
        # Copy files to fear   
        utils.fear.copy_to(data_files[-1], remote_dir + data_files[-1].split('/')[-1], fear)
#        fear.copy_to(write_files[-1], remote_dir + write_files[-1].split('/')[-1])
        
        # Create MATLAB code
        code = gpml.OPTIMIZE_KERNEL_CODE % {'datafile': data_files[-1].split('/')[-1],
                                            'writefile': write_files[-1].split('/')[-1],
                                            'gpml_path': config.FEAR_GPML_PATH,
                                            'kernel_family': kernel.gpml_kernel_expression(),
                                            'kernel_params': '[ %s ]' % ' '.join(str(p) for p in kernel.param_vector()),
                                            'noise': str(noise),
                                            'iters': str(iters)}
        
        # Submit this to fear and save the file names
        script_file, shell_file, job_id = qsub_matlab_code(code=code, verbose=verbose, local_dir=local_dir, remote_dir=remote_dir, fear=fear)
        script_files.append(script_file)
        shell_files.append(shell_file)
        job_ids.append(job_id)
        
    # Wait for and read in results
    fear_finished = False
    job_finished = [False] * len(write_files)
    results = [None] * len(write_files)

    while not fear_finished:
        job_status = utils.fear.qstat_status()
        for (i, write_file) in enumerate(write_files):
            if not job_finished[i]:
                if utils.fear.job_terminated(job_ids[i], status=job_status, fear=fear):
                    if not utils.fear.file_exists(remote_dir + write_file.split('/')[-1], fear):
                        # Job has finished but no output - re-submit
                        print 'Shell script %s job_id %s failed, re-submitting...' % (shell_files[i], job_ids[i])
                        job_ids[i] = utils.fear.qsub(shell_files[i], verbose=verbose, fear=fear)
                    else:
                        # Another job has finished
                        job_finished[i] = True
                        # Copy files
                        os.remove(write_file) # Not sure if necessary
                        utils.fear.copy_from(remote_dir + write_file.split('/')[-1], write_file, fear)
                        # Read results ##### THIS WILL CHANGE IF RUNNING DIFFERENT TYPE OF EXPERIMENT
                        gpml_result = scipy.io.loadmat(write_file)
                        optimized_hypers = gpml_result['hyp_opt']
                        nll = gpml_result['best_nll'][0, 0]
                        hessian = gpml_result['hessian']
                        assert isinstance(hessian, np.ndarray)  # just to make sure
                        laplace_nle = laplace_approx(nll, optimized_hypers, hessian, PRIOR_VAR)
                        kernel_hypers = optimized_hypers['cov'][0, 0].ravel()
                        noise_hyp = optimized_hypers['lik'][0, 0].ravel()
                        k_opt = kernels[i].family().from_param_vector(kernel_hypers)
                        BIC = 2 * nll + len(kernel_hypers) * np.log(y.shape[0])
                        results[i] = ScoredKernel(k_opt, nll, laplace_nle, BIC, noise_hyp) 
                        # Tidy up
                        utils.fear.rm(remote_dir + data_files[i].split('/')[-1], fear)
                        utils.fear.rm(remote_dir + write_files[i].split('/')[-1], fear)
                        utils.fear.rm(remote_dir + script_files[i].split('/')[-1], fear)
                        utils.fear.rm(remote_dir + shell_files[i].split('/')[-1], fear)
                        utils.fear.rm(remote_dir + shell_files[i].split('/')[-1] + '*', fear)
                        os.remove(data_files[i])
                        os.remove(write_files[i])
                        os.remove(script_files[i])
                        os.remove(shell_files[i])
                        # Tell the world
                        if verbose:
                            print '%d / %d jobs complete' % (sum(job_finished), len(job_finished))
                elif not (utils.fear.job_queued(job_ids[i], status=job_status, fear=fear) or utils.fear.job_running(job_ids[i], status=job_status, fear=fear)):
                    # Job has some status other than running or queuing - something is wrong, delete and re-submit
                    utils.fear.qdel(job_ids[i], fear=fear)
                    print 'Shell script %s job_id %s stuck, deleting and re-submitting...' % (shell_files[i], job_ids[i])
                    job_ids[i] = utils.fear.qsub(shell_files[i], verbose=verbose, fear=fear)
        
        if all(job_finished):
            fear_finished = True    
        if not fear_finished:
            # Count how many jobs are queued
            n_queued = len([1 for job_id in job_ids if utils.fear.job_queued(job_id, status=job_status, fear=fear)])
            # Count how many jobs are running
            n_running = len([1 for job_id in job_ids if utils.fear.job_running(job_id,status=job_status, fear=fear)])
            if verbose:
                print '%d jobs running' % n_running
                print '%d jobs queued' % n_queued
                print 'Sleeping'
                time.sleep(re_submit_wait)
            
    fear.close()
    
    return results            

def parse_all_results():
    entries = [];
    rownames = [];
    
    colnames = ['Dataset', 'NLL', 'Kernel' ]
    for rt in gen_all_results():
        print "dataset: %s kernel: %s\n" % (rt[0], rt[-1].pretty_print())
        entries.append(['%4.1f' % rt[1], rt[-1].latex_print()])
        rownames.append(rt[0])
    
    utils.latex.table('../latex/tables/kernels.tex', rownames, colnames, entries)

def gen_all_results():
    '''Look through all the files in the results directory'''
    for r,d,f in os.walk(config.RESULTS_PATH):
        for files in f:
            if files.endswith(".txt"):
                results_filename = os.path.join(r,files)
                best_tuple = parse_results( results_filename )
                yield files.split('.')[-2], best_tuple
                
def parse_results( results_filename ):
    result_tuples = [ScoredKernel.parse_results_string(line.strip()) for line in open(results_filename) if line.startswith("nll=")]
    best_tuple = sorted(result_tuples, key=ScoredKernel.score)[0]
    return best_tuple
       

def gen_all_kfold_datasets():
    '''Look through all the files in the results directory'''
    for r,d,f in os.walk("../data/kfold_data/"):
        for files in f:
            if files.endswith(".mat"):
                yield r, files.split('.')[-2]

def main():
    data_file = sys.argv[1];
    results_filename = sys.argv[2];
    max_depth = int(sys.argv[3]);
    k = int(sys.argv[4]);
    
    print 'Datafile=%s' % data_file
    print 'results_filename=%s' % results_filename
    print 'max_depth=%d' % max_depth
    print 'k=%d' % k
    
    #experiment(data_file, results_filename, max_depth=max_depth, k=k)    
    
def run_all_kfold():
    for r, files in gen_all_kfold_datasets():
        datafile = os.path.join(r,files + ".mat")
        output_file = os.path.join(config.RESULTS_PATH, files + "_result.txt")
        prediction_file = os.path.join(config.RESULTS_PATH, files + "_predictions.mat")
        
        fear_experiment(datafile, output_file, max_depth=4, k=3, description = 'Dave test')
        
        #k_opt, nll, laplace_nle, BIC, noise_hyp = parse_results(output_file)
        #gpml.make_predictions(k_opt.gpml_kernel_expression(), k_opt.param_vector(), datafile, prediction_file, noise_hyp, iters=30)        
        
        print "Done one file!!!"   
    
def run_test_kfold():
    
    datafile = '../data/kfold_data/r_pumadyn512_fold_3_of_10.mat'
    output_file = config.RESULTS_PATH + '/r_pumadyn512_fold_3_of_10_result.txt'
    #fear_experiment(datafile, output_file, max_depth=1, k=1, description = 'Dave test')
    
    nll, bic, laplace, kernel, noise = parse_results(output_file)
    
    prediction_file = 'AAAA'
    gpml.make_predictions(kernel.gpml_kernel_expression(), kernel.param_vector(), datafile, prediction_file, noise, iters=30)
    
    
if __name__ == '__main__':
    #main()

    run_test_kfold()
    #run_all_kfold()

