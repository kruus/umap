import numpy as np
import numba
import umap.distances as dist
from umap.utils import tau_rand_int
import umap.constraints as con

layout_version = 4
from tqdm.auto import tqdm


@numba.njit()
def clip(val):
    """Standard clamping of a value into a fixed range (in this case -4.0 to
    4.0)

    Parameters
    ----------
    val: float
        The value to be clamped.

    Returns
    -------
    The clamped value, now fixed to be in the range -4.0 to 4.0.
    """
    if val > 4.0:
        return 4.0
    elif val < -4.0:
        return -4.0
    else:
        return val

# This is very slow
#@numba.njit()
#def clip_array(arr):
#    """ clip array elementwise.  This is MUCH SLOWER than using scalar clip(val) ! """
#    return np.where( arr > 4.0, 4.0,
#                    np.where(arr < -4.0, -4.0, arr))
#    #for d in arr.shape[0]:
#    #    arr[d] = 4.0 if arr[d] > 4.0 else -4.0 if arr[d] < -4.0 else arr[d]

@numba.njit(
    "f4(f4[::1],f4[::1])",
    fastmath=True,
    cache=True,
    inline='always',
    locals={
        "result": numba.types.float32,
        "diff": numba.types.float32,
        "dim": numba.types.intp,
        "d": numba.types.int32,
    },
)
def rdist(x, y):
    """Reduced Euclidean distance.

    Parameters
    ----------
    x: array of shape (embedding_dim,)
    y: array of shape (embedding_dim,)

    Returns
    -------
    The squared euclidean distance between x and y
    """
    result = 0.0
    dim = x.shape[0]
    for d in range(dim):
        diff = x[d] - y[d]
        result += diff * diff

    return result

if False: # 'chaining' multiple constraints is perhaps too complicated

    # Workaround:
    # compose tuple of jitted fns(idx,pt)
    # This avoid numba 0.53 "experimental" passing functions warnings.
    #
    # When numba 0.53 is required, this hackery is not required
    # ref: https://github.com/numba/numba/issues/3405  and older 2542
    # 
    @numba.njit()
    def _fn_idx_pt_noop(idx,pt):
        return None
    def _chain_idx_pt(fs, inner=None):
        """for f(idx,pt) in tuple fs, invoke them in order"""
        if len(fs) == 0:
            assert inner is None
            return _fn_idx_pt_noop
        head = fs[0]
        if inner is None:
            @numba.njit()
            def wrap(idx,pt):
                head(idx,pt)
            return wrap
        else:
            @numba.njit()
            def wrap(idx,pt):
                inner(idx,pt)
                head(idx,pt)
        if len(fs) > 1:
            tail = fs[1:]
            return _chain_idx_pt(tail, wrap)
        else:
            return wrap

    # Now to chain jit fns that don't need an index
    # Ex. numba wrappers for dimlohi_pt(pt, los, his) in constraints.py
    @numba.njit()
    def _fn_pt_noop(pt):
        return None
    def _chain_pt(fs, inner=None):
        """for f(idx,pt) in tuple fs, invoke them in order"""
        if len(fs) == 0:
            assert inner is None
            return _fn_pt_noop
        head = fs[0]
        if inner is None:
            @numba.njit()
            def wrap(pt):
                head(pt)
        else:
            @numba.njit()
            def wrap(pt):
                inner(pt)
                head(pt)
        if len(fs) > 1:
            tail = fs[1:]
            return _chain_pt(tail, wrap)
        else:
            return wrap
    #

#
# apply_grad -- it is MUCH faster when inlined
#            -- it is only used 4 times (but gives clearer code)
# for code clarity ... reduce code duplication
# Is numba jit able to elide "is not None" code blocks?
#    Quick tests show that using 'None' (or optional), numba may fail
#    to elide the code block :(  (used env NUMBA_DEBUG to check simple cases)
#@numba.njit() # use to inspect
@numba.njit(inline='always')
def apply_grad(idx, pt, alpha, grad, fn_idx_grad, fn_grad, fn_idx_pt, fn_pt):
    """ updates pt by (projected?) grad and any pt projection functions.

    idx:    point number
    pt:     point vector
    alpha:  learning rate
    grad:   gradient vector
    fn_...: constraint functions (or None)
    
    Both pt and grad may be modified.
    """
    # can i get _doclip as a constant?
    _doclip = False
    #_doclip = (fn_idx_grad is not None or fn_grad is not None)
    #_doclip = isinstance(fn_idx_grad, types.NoneType) or isinstance(fn_grad,  types.NoneType)
    if fn_idx_grad is not None:
        fn_idx_grad(idx, pt, grad)  # grad (pt?) may change
        _doclip = True
    if fn_grad is not None:
        fn_grad(pt, grad)         # grad (pt?) may change
        _doclip = True
    if _doclip:
        for d in range(pt.shape[0]):
            pt[d] = pt[d] + alpha * clip(grad[d])
    else:
        for d in range(pt.shape[0]):
            pt[d] = pt[d] + alpha * grad[d]

    if fn_idx_pt is not None:
        fn_idx_pt(idx, pt) # Hmmm. if pt just changed, we've forgotten where it came "from"!
    if fn_pt is not None:
        fn_pt(pt)
    # no return value -- pt and grad mods are IN-PLACE.

# this is a reasonable parallel=False version, easy to read
# (not enabled?)
def _optimize_layout_euclidean_single_epoch_applygrad(
    head_embedding,
    tail_embedding,
    head,
    tail,
    n_vertices,
    epochs_per_sample,
    a,
    b,
    rng_state,
    gamma,
    dim,
    move_other,
    alpha,
    epochs_per_negative_sample,
    epoch_of_next_negative_sample,
    epoch_of_next_sample,
    n,              # epoch
    wrap_idx_pt,    # NEW: constraint fn(idx,pt) or None
    wrap_idx_grad,  #      constraint fn(idx,pt,grad) or None
    wrap_pt,        #      constraint fn(pt) or None
    wrap_grad,      #      constraint fn(pt,grad) or None
    densmap_flag,
    dens_phi_sum,
    dens_re_sum,
    dens_re_cov,
    dens_re_std,
    dens_re_mean,
    dens_lambda,
    dens_R,
    dens_mu,
    dens_mu_tot,
):
    #print("opt Euc applygrad")
    #verbose=2
    grad_d     = np.empty(dim, dtype=head_embedding.dtype)
    other_grad = np.empty(dim, dtype=head_embedding.dtype)
    for i in range(epochs_per_sample.shape[0]):
        if epoch_of_next_sample[i] <= n:
            j = head[i]
            k = tail[i]

            current = head_embedding[j]
            other = tail_embedding[k]

            dist_squared = rdist(current, other)

            if densmap_flag:
                # Note that densmap could, if you wish, be viewed as a
                # "constraint" in that it's a "gradient modification function"
                phi = 1.0 / (1.0 + a * pow(dist_squared, b))
                dphi_term = (
                    a * b * pow(dist_squared, b - 1) / (1.0 + a * pow(dist_squared, b))
                )

                q_jk = phi / dens_phi_sum[k]
                q_kj = phi / dens_phi_sum[j]

                drk = q_jk * (
                    (1.0 - b * (1 - phi)) / np.exp(dens_re_sum[k]) + dphi_term
                )
                drj = q_kj * (
                    (1.0 - b * (1 - phi)) / np.exp(dens_re_sum[j]) + dphi_term
                )

                re_std_sq = dens_re_std * dens_re_std
                weight_k = (
                    dens_R[k]
                    - dens_re_cov * (dens_re_sum[k] - dens_re_mean) / re_std_sq
                )
                weight_j = (
                    dens_R[j]
                    - dens_re_cov * (dens_re_sum[j] - dens_re_mean) / re_std_sq
                )

                grad_cor_coeff = (
                    dens_lambda
                    * dens_mu_tot
                    * (weight_k * drk + weight_j * drj)
                    / (dens_mu[i] * dens_re_std)
                    / n_vertices
                )

            if dist_squared > 0.0:
                grad_coeff = -2.0 * a * b * pow(dist_squared, b - 1.0)
                grad_coeff /= a * pow(dist_squared, b) + 1.0
            else:
                grad_coeff = 0.0

            # original attractivie-update loop
            #for d in range(dim):
            #    grad_d = clip(grad_coeff * (current[d] - other[d]))
            #
            #    if densmap_flag:
            #        grad_d += clip(2 * grad_cor_coeff * (current[d] - other[d]))
            #
            #    current[d] += grad_d * alpha
            #    if move_other:
            #        other[d] += -grad_d * alpha
            #
            # replacement: grad_d vector might be projected onto tangent space
            if densmap_flag:
                for d in range(dim):
                    gd = clip((grad_coeff + 2*grad_cor_coeff)
                              * (current[d]-other[d]))
                    grad_d[d]     = gd
                    other_grad[d] = -gd
            else:
                for d in range(dim):
                    gd = clip(grad_coeff * (current[d]-other[d]))
                    grad_d[d]     = gd
                    other_grad[d] = -gd

            #if verbose >= 2: print("apply_grad j",j)
            apply_grad(j, current, alpha, grad_d,
                        wrap_idx_grad, wrap_grad, wrap_idx_pt, wrap_pt)

            if move_other:
                #if verbose >= 2: print("move_other k",k)
                apply_grad(k, other, alpha, other_grad,
                            wrap_idx_grad, wrap_grad, wrap_idx_pt, wrap_pt)

            epoch_of_next_sample[i] += epochs_per_sample[i]

            n_neg_samples = int(
                (n - epoch_of_next_negative_sample[i]) / epochs_per_negative_sample[i]
            )

            if n_neg_samples > 0: # I think this should be here
                for p in range(n_neg_samples):
                    k = tau_rand_int(rng_state) % n_vertices

                    other = tail_embedding[k]

                    dist_squared = rdist(current, other)

                    if dist_squared > 0.0:
                        grad_coeff = 2.0 * gamma * b
                        grad_coeff /= (0.001 + dist_squared) * (
                            a * pow(dist_squared, b) + 1
                        )
                    elif j == k:
                        continue
                    else:
                        grad_coeff = 0.0

                    if grad_coeff > 0.0:
                        for d in range(dim):
                            gd = clip(grad_coeff * (current[d] - other[d]))
                            grad_d[d]     = gd
                            other_grad[d] = -gd
                    else:
                        for d in range(dim):
                            grad_d[d]     = 4.0
                            other_grad[d] = -4.0

                    #if verbose >= 2: print("negsample j",j)
                    apply_grad(j, current, alpha, grad_d,
                                wrap_idx_grad, wrap_grad, wrap_idx_pt, wrap_pt)

                    # following is needed for correctness if tail==head
                    if move_other:
                        #if verbose >= 2: print("negsample k",k)
                        apply_grad(k, other, alpha, other_grad,
                                    wrap_idx_grad, wrap_grad, wrap_idx_pt, wrap_pt)

                epoch_of_next_negative_sample[i] += (
                    n_neg_samples * epochs_per_negative_sample[i]
                )
        # END if epoch_of_next_sample[i] <= n:
    # END for i in numba.prange(epochs_per_sample.shape[0]):

# use ONLY with parallel=False
def _optimize_layout_euclidean_single_epoch(
    head_embedding,
    tail_embedding,
    head,
    tail,
    n_vertices,
    epochs_per_sample,
    a,
    b,
    rng_state,
    gamma,
    dim,
    move_other,
    alpha,
    epochs_per_negative_sample,
    epoch_of_next_negative_sample,
    epoch_of_next_sample,
    n,              # epoch
    wrap_idx_pt,    # NEW: constraint fn(idx,pt) or None
    wrap_idx_grad,  #      constraint fn(idx,pt,grad) or None
    wrap_pt,        #      constraint fn(pt) or None
    wrap_grad,      #      constraint fn(pt,grad) or None
    densmap_flag,
    dens_phi_sum,
    dens_re_sum,
    dens_re_cov,
    dens_re_std,
    dens_re_mean,
    dens_lambda,
    dens_R,
    dens_mu,
    dens_mu_tot,
):
    # catch simplifying assumptions
    #   TODO: Work through constraint correctness when these fail!
    assert head_embedding.shape == tail_embedding.shape  # nice for constraints
    assert np.all(head_embedding == tail_embedding)
    #print("opt Euc optimized")

    _grad_mods = wrap_idx_grad is not None or wrap_grad is not None
    # naah -- this did not remedy removal of neg sample 'move_other' code
    #grad_four = 4.0  # toggle between 4.0 and -4.0 to "randomize" grad_coef==0.0 moves

    # This ONLY works for numba parallel=False
    grad_d     = np.empty(dim, dtype=head_embedding.dtype) #one per thread!
    other_grad = np.empty(dim, dtype=head_embedding.dtype)
    for i in numba.prange(epochs_per_sample.shape[0]):

        if epoch_of_next_sample[i] <= n:
            j = head[i]
            k = tail[i]

            current = head_embedding[j]
            other = tail_embedding[k]

            dist_squared = rdist(current, other)

            if dist_squared > 0.0:
                grad_coeff = -2.0 * a * b * pow(dist_squared, b - 1.0)
                grad_coeff /= a * pow(dist_squared, b) + 1.0
            else:
                grad_coeff = 0.0

            if densmap_flag:
                # Note that densmap could, if you wish, be viewed as a
                # "constraint" in that it's a "gradient modification function"
                phi = 1.0 / (1.0 + a * pow(dist_squared, b))
                dphi_term = (
                    a * b * pow(dist_squared, b - 1) / (1.0 + a * pow(dist_squared, b))
                )

                q_jk = phi / dens_phi_sum[k]
                q_kj = phi / dens_phi_sum[j]

                drk = q_jk * (
                    (1.0 - b * (1 - phi)) / np.exp(dens_re_sum[k]) + dphi_term
                )
                drj = q_kj * (
                    (1.0 - b * (1 - phi)) / np.exp(dens_re_sum[j]) + dphi_term
                )

                re_std_sq = dens_re_std * dens_re_std
                weight_k = (
                    dens_R[k]
                    - dens_re_cov * (dens_re_sum[k] - dens_re_mean) / re_std_sq
                )
                weight_j = (
                    dens_R[j]
                    - dens_re_cov * (dens_re_sum[j] - dens_re_mean) / re_std_sq
                )

                grad_cor_coeff = (
                    dens_lambda
                    * dens_mu_tot
                    * (weight_k * drk + weight_j * drj)
                    / (dens_mu[i] * dens_re_std)
                    / n_vertices
                )

                # Reorganize double-clip of original umap impl to a simpler
                # densmap update of grad_coeff (result is a bit different, since
                # only a single 'clip' gets applied)
                grad_coeff += 2.0*grad_cor_coeff

            if move_other:
                if _grad_mods:
                    for d in range(dim):
                        grad_d[d] = grad_coeff * (current[d] - other[d]) # no clip
                        other_grad[d] = -grad_d[d]
                    if wrap_idx_grad is not None:
                        wrap_idx_grad(j, current, grad_d)
                        wrap_idx_grad(k, other, other_grad)
                    if wrap_grad is not None:
                        wrap_grad(current, grad_d)
                        wrap_grad(other, other_grad)
                    for d in range(dim): # post-constraint gradient alpha*clip
                        current[d] += alpha * clip(grad_d[d])
                        other[d] += alpha * clip(other_grad[d])
                else:
                    for d in range(dim):
                        delta = alpha * clip(grad_coeff * (current[d] - other[d])) # clip
                        current[d] += delta
                        other[d] -= delta

                if wrap_idx_pt is not None:
                    wrap_idx_pt(j, current)
                    wrap_idx_pt(k, other)
                if wrap_pt is not None:
                    wrap_pt(current)
                    wrap_pt(other)

                # tentatively perhaps should also do...
                epoch_of_next_sample[k] += epochs_per_sample[k]
            else:
                if _grad_mods:
                    for d in range(dim):
                        grad_d[d] = grad_coeff * (current[d] - other[d])
                    if wrap_idx_grad is not None:
                        wrap_idx_grad(j, current, grad_d)
                    if wrap_grad is not None:
                        wrap_grad(current, grad_d)
                    for d in range(dim):
                        current[d] += alpha * clip(grad_d[d])
                else:
                    for d in range(dim):
                        current[d] += alpha * clip(grad_coeff * (current[d] - other[d]))

                if wrap_idx_pt is not None:
                    wrap_idx_pt(j, current)
                if wrap_pt is not None:
                    wrap_pt(current)

            epoch_of_next_sample[i] += epochs_per_sample[i]

            n_neg_samples = int(
                (n - epoch_of_next_negative_sample[i]) / epochs_per_negative_sample[i]
            )

            # NEW [ejk]  this seems to make sense
            if n_neg_samples <= 0:
                continue

            # NEXT IDEA: loop over 'p' ACCUMULATES clip(grad_d) (only)
            #   AFTER loop, apply constraints, reclip and move 'current'
            #   (other_grad, if enabled here, operates for each 'k' immediately
            for p in range(n_neg_samples):
                k = tau_rand_int(rng_state) % n_vertices

                other = tail_embedding[k]

                dist_squared = rdist(current, other)

                if dist_squared > 0.0:
                    grad_coeff = 2.0 * gamma * b
                    grad_coeff /= (0.001 + dist_squared) * (
                        a * pow(dist_squared, b) + 1
                    )
                elif j == k:
                    continue
                else:
                    grad_coeff = 0.0

                #if move_other: # stock umap NEVER does this...
                if False:
                    #
                    # Divergence:
                    #  move_other seems needed for correctness if tail==head,
                    #  but stock umap does NOT do any move_other for other random
                    #  samples.  Maybe to decrease bad things in parallel=True? Dunno.
                    #
                    #  If removed, the with constraints some clusters can go to
                    #  'outside' the constraint boundaries, when you really expect
                    #  unconstrainged points to lie between two 'extreme' embeddings
                    #  for a cluster.
                    #
                    #  This can still happen sometimes, even with move_other for neg samples.
                    #
                    if _grad_mods:
                        if grad_coeff > 0.0:
                            for d in range(dim):
                                grad_d[d] = grad_coeff * (current[d] - other[d])
                                other_grad[d] = -grad_d[d]
                        else:
                            for d in range(dim):
                                grad_d[d] = 4.0
                                other_grad[d] = -4.0
                            #grad_four = -grad_four

                        if wrap_idx_grad is not None:
                            wrap_idx_grad(j, current, grad_d)
                            wrap_idx_grad(k, other, other_grad)
                        if wrap_grad is not None:
                            wrap_grad(current, grad_d)
                            wrap_grad(other, other_grad)
                        for d in range(dim):
                            current[d] += alpha * clip(grad_d[d])
                            other[d] += alpha * clip(other_grad[d])
                    else:
                        if grad_coeff > 0.0:
                            for d in range(dim):
                                delta = alpha * clip(grad_coeff * (current[d]-other[d]))
                                current[d] += delta
                                other[d] -= delta
                        else:
                            for d in range(dim):
                                delta = alpha * 4.0
                                current[d] += delta
                                other[d] -= delta
                            #grad_four = -grad_four

                    if wrap_idx_pt is not None:
                        wrap_idx_pt(j, current)
                        wrap_idx_pt(k, other)
                    if wrap_pt is not None:
                        wrap_pt(current)
                        wrap_pt(other)
                    # Hmmm. technically we should bump epoch_of_next_negative_sample[k] ??
                    # much slower
                    #epoch_of_next_negative_sample[k] += epochs_per_negative_sample[k]
                else:
                    if _grad_mods:
                        if grad_coeff > 0.0:
                            for d in range(dim):
                                grad_d[d] = grad_coeff * (current[d] - other[d])
                        else:
                            for d in range(dim):
                                grad_d[d] = 4.0
                            #grad_four = -grad_four
                        if wrap_idx_grad is not None:
                            wrap_idx_grad(j, current, grad_d)
                        if wrap_grad is not None:
                            wrap_grad(current, grad_d)
                        for d in range(dim):
                            #grad_d[d] = clip(grad_d[d])
                            current[d] += alpha * clip(grad_d[d])
                    else:
                        if grad_coeff > 0.0:
                            for d in range(dim):
                                current[d] += alpha * clip(grad_coeff * (current[d]-other[d]))
                        else:
                            for d in range(dim):
                                current[d] += alpha * 4.0
                            #grad_four = -grad_four
                    #current += alpha * grad_d
                    #for d in range(dim):
                    #    current[d] += alpha * grad_d[d]
                    if wrap_idx_pt is not None:
                        wrap_idx_pt(j, current)
                    if wrap_pt is not None:
                        wrap_pt(current)

            # with 'move_other' for negative samples should this incr by 0.5*n_neg_samples^2 ?
            #n_neg_eff = n_neg_samples
            #n_neg_eff = n_neg_samples * n_neg_samples // 2
            epoch_of_next_negative_sample[i] += (
                n_neg_samples * epochs_per_negative_sample[i]
            )
        # END if epoch_of_next_sample[i] <= n:
    # END for i in numba.prange(epochs_per_sample.shape[0]):


# parallel=True
#
# use a shared global per-thread alloc outside the numba prange
#
# (Finally faster for iris X[150,4] data, ~ 2x for 8 cpu laptop)
# It uses a work area shared between threads, and uses a numba internal
# to give 'current thread number' (ok for tbb or omp numba backends only)
def _optimize_layout_euclidean_single_epoch_para(
    head_embedding,
    tail_embedding,
    head,
    tail,
    n_vertices,
    epochs_per_sample,
    a,
    b,
    rng_state,
    gamma,
    dim,
    move_other,
    alpha,
    epochs_per_negative_sample,
    epoch_of_next_negative_sample,
    epoch_of_next_sample,
    n,              # epoch
    wrap_idx_pt,    # NEW: constraint fn(idx,pt) or None
    wrap_idx_grad,  #      constraint fn(idx,pt,grad) or None
    wrap_pt,        #      constraint fn(pt) or None
    wrap_grad,      #      constraint fn(pt,grad) or None
    densmap_flag,
    dens_phi_sum,
    dens_re_sum,
    dens_re_cov,
    dens_re_std,
    dens_re_mean,
    dens_lambda,
    dens_R,
    dens_mu,
    dens_mu_tot,
):
    assert head_embedding.shape == tail_embedding.shape  # nice for constraints
    assert np.all(head_embedding == tail_embedding)
    #print("opt Euc ||")
    #verbose=2

    #
    # thread-local memory area[s]:
    #
    nthr = numba.get_num_threads() # if parallel, provide non-overlapped scratch areas
    #dimx = (2*dim+1024 + 1024-1) // 1024 # 2*dim pts rounded up to mult of 1024
    #tmp = np.empty( (nthr, dimx), dtype=head_embedding.dtype ) # can do 2d or 1d tmp
    # But couldn't co-locate succesfully and pass a "right-sized view" into the tmp space
    # SO... just do 2 separate allocs
    dimx = (dim+1024 + 1024-1) // 1024 # 1*dim pts rounded up to mult of 1024
    tmp1 = np.empty( (nthr, dimx), dtype=head_embedding.dtype )
    if move_other:
        tmp2 = np.empty( (nthr, dimx), dtype=head_embedding.dtype )

    # reclip gradients *after* gradient-modifying constraints?
    _grad_mods = wrap_idx_grad is not None or wrap_grad is not None

    for i in numba.prange(epochs_per_sample.shape[0]):
        if epoch_of_next_sample[i] > n:
            continue

        # Approach #1:
        # this is NOT one per thread, unfortunately
        #grad_d     = np.empty(dim, dtype=head_embedding.dtype)
        #other_grad = np.empty(dim, dtype=head_embedding.dtype)
        # This thread writes into these memory locations:
        thr = numba.np.ufunc._get_thread_id()   # for tbb or omp backed, gives {0,1,...}
        # try out "view" approach 1st:
        #grad_d = tmp[ thr, 0:dim ]
        #other_grad = tmp[ thr, dim:(dim+dim) ]
        #grad_d = tmp[ (thr*dimx):(thr*dimx+dim) ]
        #other_grad = tmp[ (thr*dimx+dim):(thr*dimx+2*dim) ]

        j = head[i]
        k = tail[i]

        current = head_embedding[j]
        other = tail_embedding[k]

        dist_squared = rdist(current, other)

        if dist_squared > 0.0:
            grad_coeff = -2.0 * a * b * pow(dist_squared, b - 1.0)
            grad_coeff /= a * pow(dist_squared, b) + 1.0
        else:
            grad_coeff = 0.0

        if densmap_flag:
            # Note that densmap could, if you wish, be viewed as a
            # "constraint" in that it's a "gradient modification function"
            phi = 1.0 / (1.0 + a * pow(dist_squared, b))
            dphi_term = (
                a * b * pow(dist_squared, b - 1) / (1.0 + a * pow(dist_squared, b))
            )

            q_jk = phi / dens_phi_sum[k]
            q_kj = phi / dens_phi_sum[j]

            drk = q_jk * (
                (1.0 - b * (1 - phi)) / np.exp(dens_re_sum[k]) + dphi_term
            )
            drj = q_kj * (
                (1.0 - b * (1 - phi)) / np.exp(dens_re_sum[j]) + dphi_term
            )

            re_std_sq = dens_re_std * dens_re_std
            weight_k = (
                dens_R[k]
                - dens_re_cov * (dens_re_sum[k] - dens_re_mean) / re_std_sq
            )
            weight_j = (
                dens_R[j]
                - dens_re_cov * (dens_re_sum[j] - dens_re_mean) / re_std_sq
            )

            grad_cor_coeff = (
                dens_lambda
                * dens_mu_tot
                * (weight_k * drk + weight_j * drj)
                / (dens_mu[i] * dens_re_std)
                / n_vertices
            )

            # Reorganize double-clip of original umap impl to a simpler
            # densmap update of grad_coeff (result is a bit different, since
            # only a single 'clip' gets applied)
            grad_coeff += 2.0*grad_cor_coeff

        if move_other:
            #print("|| move_other j",j,"k",k)
            #grad_d = tmp[ thr, 0:dim ]
            #other_grad = tmp[ thr, dim:(dim+dim) ] # this is a "VIEW", so constraints are FUBAR
            # GOOD: grad_d shape is (2,)
            # OHOH other_grad.shape is (0,)
            grad_d = tmp1[ thr, 0:dim ]
            other_grad = tmp2[ thr, 0:dim ] # this is a "VIEW", so constraints are FUBAR
            #if other_grad.shape != other.shape:
            #    print("WARNING: other_grad.shape",other_grad.shape,"expected",other.shape)
            #    print("             grad_d.shape",grad_d.shape)
            if _grad_mods:
                for d in range(dim):
                    grad_d[d] = grad_coeff * (current[d]-other[d]) # without clip
                    other_grad[d] = -grad_d[d]
                if wrap_idx_grad is not None:
                    wrap_idx_grad(j, current, grad_d)
                    wrap_idx_grad(k, other, other_grad)
                if wrap_grad is not None:
                    wrap_grad(current, grad_d)
                    wrap_grad(other, other_grad)
                for d in range(dim): # post-constraint gradient clip
                    current[d] += alpha * clip(grad_d[d])
                    other[d] += alpha * clip(other_grad[d])
            else:
                for d in range(dim):
                    delta = alpha * clip(grad_coeff * (current[d]-other[d]))
                    current[d] += delta
                    other[d] -= delta
            #current += alpha * grad_d
            #for d in range(dim):
            #    current[d] += grad_d[d]
            #    other[d] += other_grad[d]
            if wrap_idx_pt is not None:
                wrap_idx_pt(j, current)
                wrap_idx_pt(k, other)
            if wrap_pt is not None:
                wrap_pt(current)
                wrap_pt(other)
        else:
            #print("|| j",j)
            #grad_d = tmp[ thr, 0:dim ]
            grad_d = tmp1[ thr, 0:dim ]
            if _grad_mods:
                for d in range(dim):
                    grad_d[d] = grad_coeff * (current[d]-other[d]) # elide clip here
                if wrap_idx_grad is not None:
                    wrap_idx_grad(j, current, grad_d)
                if wrap_grad is not None:
                    wrap_grad(current, grad_d)
                for d in range(dim): # post-constraint gradient clip
                    current[d] += alpha * clip(grad_d[d])
            else:
                for d in range(dim):
                    current[d] += alpha * clip(grad_coeff * (current[d]-other[d]))
            if wrap_idx_pt is not None:
                wrap_idx_pt(j, current)
            if wrap_pt is not None:
                wrap_pt(current)

        epoch_of_next_sample[i] += epochs_per_sample[i]

        n_neg_samples = int(
            (n - epoch_of_next_negative_sample[i]) / epochs_per_negative_sample[i]
        )

        if n_neg_samples <= 0:
            continue

        for p in range(n_neg_samples):
            k = tau_rand_int(rng_state) % n_vertices

            other = tail_embedding[k]

            dist_squared = rdist(current, other)

            if dist_squared > 0.0:
                grad_coeff = 2.0 * gamma * b
                grad_coeff /= (0.001 + dist_squared) * (
                    a * pow(dist_squared, b) + 1
                )
            elif j == k:
                continue
            else:
                grad_coeff = 0.0

            #if move_other: # stock umap NEVER does this...
            if False:
                #print("|| neg j",j,"k",k)
                #
                # Note: stock umap NEVER does move_other for -ve samples
                #
                # Divergence:
                #  move_other here is needed for correctness if tail==head,
                #  but stock umap does NOT do any move_other for other random
                #  samples.  Maybe to decrease bad things in parallel=True? Dunno.
                #
                if _grad_mods:
                    if grad_coeff>0.0:
                        for d in range(dim):
                            grad_d[d] = grad_coeff * (current[d]-other[d])
                            other_grad[d] = -grad_d[d]
                    else:
                        for d in range(dim):
                            grad_d[d] = 4.0
                            other_grad[d] = -4.0
                    if wrap_idx_grad is not None:
                        wrap_idx_grad(j, current, grad_d)
                        wrap_idx_grad(k, other, other_grad)
                    if wrap_grad is not None:
                        wrap_grad(current, grad_d)
                        wrap_grad(other, other_grad)
                    for d in range(dim): # also a post-constraint clip
                        current[d] += alpha * clip(grad_d[d])
                        other[d] += alpha * clip(other_grad[d])
                else:
                    if grad_coeff>0.0:
                        for d in range(dim):
                            delta = alpha * clip(grad_coeff * (current[d]-other[d]))
                            current[d] += delta
                            other_grad[d] -= delta
                    else:
                        for d in range(dim):
                            delta = alpha * 4.0
                            current[d] += delta
                            other[d] -= delta
                #current += alpha * grad_d
                #for d in range(dim): # possible rare read-tear in parallel
                #    current[d] += grad_d[d]
                #    other[d] += other_grad[d]
                if wrap_idx_pt is not None:
                    wrap_idx_pt(j, current)
                    wrap_idx_pt(k, other)
                if wrap_pt is not None:
                    wrap_pt(current)
                    wrap_pt(other)

                # Technically, might argue for:
                #epoch_of_next_negative_sample[k] += epochs_per_negative_sample[k]
            else:
                #print("|| neg j",j)
                if _grad_mods:
                    if grad_coeff > 0.0:
                        for d in range(dim):
                            grad_d[d] = grad_coeff * (current[d]-other[d])
                    else:
                        for d in range(dim):
                            grad_d[d] = 4.0
                    if wrap_idx_grad is not None:
                        wrap_idx_grad(j, current, grad_d)
                    if wrap_grad is not None:
                        wrap_grad(current, grad_d)
                    for d in range(dim): # also a post-constraint clip
                        current[d] += alpha * clip(grad_d[d])
                else:
                    if grad_coeff > 0.0:
                        for d in range(dim):
                            current[d] += alpha * clip(grad_coeff * (current[d]-other[d]))
                    else:
                        for d in range(dim):
                            current[d] += alpha * 4.0
                #current += alpha * grad_d
                #for d in range(dim): # read-tear possible in parallel
                #    current[d] += grad_d[d]
                if wrap_idx_pt is not None:
                    wrap_idx_pt(j, current)
                if wrap_pt is not None:
                    wrap_pt(current)

        epoch_of_next_negative_sample[i] += (
            n_neg_samples * epochs_per_negative_sample[i]
        )
        # END if epoch_of_next_sample[i] <= n:
    # END for i in numba.prange(epochs_per_sample.shape[0]):



#opt_euclidean = numba.njit(_optimize_layout_euclidean_single_epoch_applygrad,
#                            fastmath=True, parallel=False,)
opt_euclidean = numba.njit(_optimize_layout_euclidean_single_epoch,
                            fastmath=True, parallel=False,)

opt_euclidean_para = numba.njit(_optimize_layout_euclidean_single_epoch_para,
                                 fastmath=True, parallel=True, nogil=True)

@numba.njit()
def _optimize_layout_euclidean_densmap_epoch_init(
    head_embedding,
    tail_embedding,
    head,
    tail,
    a,
    b,
    re_sum,
    phi_sum,
):
    re_sum.fill(0)
    phi_sum.fill(0)

    for i in numba.prange(head.size):
        j = head[i]
        k = tail[i]

        current = head_embedding[j]
        other = tail_embedding[k]
        dist_squared = rdist(current, other)

        phi = 1.0 / (1.0 + a * pow(dist_squared, b))

        re_sum[j] += phi * dist_squared
        re_sum[k] += phi * dist_squared
        phi_sum[j] += phi
        phi_sum[k] += phi

    epsilon = 1e-8
    for i in range(re_sum.size):
        re_sum[i] = np.log(epsilon + (re_sum[i] / phi_sum[i]))



def optimize_layout_euclidean(
    head_embedding,
    tail_embedding,
    head,
    tail,
    n_epochs,
    n_vertices,
    epochs_per_sample,
    a,
    b,
    rng_state,
    gamma=1.0,
    initial_alpha=1.0,
    negative_sample_rate=5.0,
    parallel=False,
    verbose=False,
    densmap=False,
    densmap_kwds=None,
    tqdm_kwds=None,
    move_other=False,
    output_constrain=None, # independent of head_embedding index
    pin_mask=None, # dict of constraints (or ndarray of 0/1 mask)
):
    """Improve an embedding using stochastic gradient descent to minimize the
    fuzzy set cross entropy between the 1-skeletons of the high dimensional
    and low dimensional fuzzy simplicial sets. In practice this is done by
    sampling edges based on their membership strength (with the (1-p) terms
    coming from negative sampling similar to word2vec).
    Parameters
    ----------
    head_embedding: array of shape (n_samples, n_components)
        The initial embedding to be improved by SGD.
    tail_embedding: array of shape (source_samples, n_components)
        The reference embedding of embedded points. If not embedding new
        previously unseen points with respect to an existing embedding this
        is simply the head_embedding (again); otherwise it provides the
        existing embedding to embed with respect to.
    head: array of shape (n_1_simplices)
        The indices of the heads of 1-simplices with non-zero membership.
    tail: array of shape (n_1_simplices)
        The indices of the tails of 1-simplices with non-zero membership.
    n_epochs: int
        The number of training epochs to use in optimization.
    n_vertices: int
        The number of vertices (0-simplices) in the dataset.
    epochs_per_sample: array of shape (n_1_simplices)
        A float value of the number of epochs per 1-simplex. 1-simplices with
        weaker membership strength will have more epochs between being sampled.
    a: float
        Parameter of differentiable approximation of right adjoint functor
    b: float
        Parameter of differentiable approximation of right adjoint functor
    rng_state: array of int64, shape (3,)
        The internal state of the rng
    gamma: float (optional, default 1.0)
        Weight to apply to negative samples.
    initial_alpha: float (optional, default 1.0)
        Initial learning rate for the SGD.
    negative_sample_rate: int (optional, default 5)
        Number of negative samples to use per positive sample.
    parallel: bool (optional, default False)
        Whether to run the computation using numba parallel.
        Running in parallel is non-deterministic, and is not used
        if a random seed has been set, to ensure reproducibility.
    verbose: bool (optional, default False)
        Whether to report information on the current progress of the algorithm.
    densmap: bool (optional, default False)
        Whether to use the density-augmented densMAP objective
    densmap_kwds: dict (optional, default None)
        Auxiliary data for densMAP
    tqdm_kwds: dict (optional, default None)
        Keyword arguments for tqdm progress bar.
    move_other: bool (optional, default False)
        Whether to adjust tail_embedding alongside head_embedding
        umap_.py simplicial_set_embedding always calls with move_other=True,
        even though our default is move_other=False
    output_constrain: default None
        dict of numba point or grad submanifold projection functions
    pin_mask: default None
        Array of shape (n_samples) or (n_samples, n_components)
        The weights (in [0,1]) assigned to each sample, defining how much they
        should be updated. 0 means the point will not move at all, 1 means
        they are updated normally. In-between values allow for fine-tuning.
        A 2-D mask can supply different weights to each dimension of each sample.
        OR
        A dictionary of strings -> [constraint,...] (WIP)
    Returns
    -------
    embedding: array of shape (n_samples, n_components)
        The optimized embedding.
    """

    #verbose = True
    if verbose:
        print("optimize_layout_euclidean")
        print(type(head_embedding), head_embedding.dtype if isinstance(head_embedding, np.ndarray) else "")
        print(type(tail_embedding), tail_embedding.dtype if isinstance(tail_embedding, np.ndarray) else "")
        print(type(head), head.dtype if isinstance(head, np.ndarray) else "")
        print(type(tail), tail.dtype if isinstance(tail, np.ndarray) else "")
        print(type(n_epochs))
        print(type(epochs_per_sample), epochs_per_sample.dtype if isinstance(epochs_per_sample, np.ndarray) else "")
        print("a,b", type(a), type(b))
        print("rng_state", rng_state,type(rng_state))
        print("gamma, initial_alpha", type(gamma), type(initial_alpha))
        print("negative_sample_rate", type(negative_sample_rate))
        print("parallel, verbose, densmap", type(parallel) ,type(verbose), type(densmap))
        print("densmap_kwds", densmap_kwds)
        print("move_other", move_other)
        print("output_constrain", output_constrain)
        print("pin_mask", type(pin_mask))
        print("head,tail_emebdding shapes", head_embedding.shape, tail_embedding.shape)
        for i in range(min(150,head_embedding.shape[0])):
            print(f"head_embedding[{i}] = {head_embedding[i]}")
        if head_embedding.shape == tail_embedding.shape and np.allclose(head_embedding,tail_embedding):
            print("head_embedding ~= tail_embedding")
        else:
            print("head_embedding not ~= tail_embedding")
            for i in range(min(150,head_embedding.shape[0])):
                print(f"tail_embedding[{i}] = {tail_embedding[i]}")

        print("head,tail           shapes", head.shape, tail.shape)
        for i in range(min(150,head.shape[0])):
            print(f"head[{i}] = {head[i]}")
        if head.shape == tail.shape and np.allclose(head,tail):
            print("head ~= tail")
        else:
            for i in range(min(150,tail_embedding.shape[0])):
                print(f"tail[{i}] = {tail[i]}")

    dim = head_embedding.shape[1]
    alpha = initial_alpha

    epochs_per_negative_sample = epochs_per_sample / negative_sample_rate
    epoch_of_next_negative_sample = epochs_per_negative_sample.copy()
    epoch_of_next_sample = epochs_per_sample.copy()

    #optimize_fn = numba.njit(
    #    _optimize_layout_euclidean_single_epoch, fastmath=True, parallel=parallel
    #)

    #<<<<<<< HEAD
    if pin_mask is None:
        pin_mask = {}
    have_constraints = False
    # historical: packing a list of fns into a single call was problematic
    #             perhaps better in numba >= 0.53?
    # But eventually it would be nice to accept a list of functions
    fns_idx_pt = []
    fns_idx_grad = []

    if isinstance(pin_mask, dict): # pin_mask is a more generic "constraints" dictionary
        # Dictionary layout:
        #   key  -->   list( tuple )
        #   where tuple ~ (jit_function, [const_args,...]) (or empty)
        #print("constraints", pin_mask.keys())
        for kk,k in enumerate(pin_mask.keys()):
            if verbose: print("kk,k",kk,k)
            if k=='idx_pt':
                fns_idx_pt = [pin_mask[k]] # revert: do NOT support a list, numba issues?
                have_constraints = True
            elif k=='idx_grad':
                fns_idx_grad = [pin_mask[k]]
                have_constraints = True
            #elif k=='epoch': fns_epoch = [pin_mask[k][0]]
            else:
                print(" Warning: unrecognized constraints key", k)
                print(" Allowed constraint keys:", recognized)
        # I'm trying to avoid numba 0.53 warnings (about firstclass functions)

    else:
        print("pin_mask", pin_mask.size, pin_mask.shape)
        assert (isinstance(pin_mask, np.ndarray))
        if len(pin_mask.shape) == 1:
            assert pin_mask.shape[0] == head_embedding.shape[0]
            # let's use pinindexed_grad for this one
            # (it's a more stringent test)
            idxs = []
            for i in range(pin_mask.size):
                if pin_mask[i]==0.0:
                    idxs.append(i)
            if verbose: print("pin_mask 1d:",  len(idxs), "points don't move")
            fixed_pos_idxs = np.array(idxs)
            # todo [opt]: no-op if zero points were fixed
            @numba.njit()
            def pin_mask_1d(idx,pt,grad):
                return con.pinindexed_grad(idx,pt,grad, fixed_pos_idxs)
            fns_idx_grad = [pin_mask_1d,]
            have_constraints = True

        elif len(pin_mask.shape) == 2:
            assert pin_mask.shape == head_embedding.shape
            # DEBUG:
            #for i in range(pin_mask.shape[0]):
            #    for d in range(dim):
            #        if pin_mask[i,d] == 0.0:
            #            print("sample",i,"pin head[",d,"] begins at",head_embedding[i,d])
            # v.2 translates zeros in pin_mask to embedding values; nonzeros become np.inf
            # and then uses a 'freeinf' constraint from constraints.py
            # Current fixed point dimensions are copied into the constraint itself.
            freeinf_arg = np.where( pin_mask == 0.0, head_embedding, np.float32(np.inf) )
            if verbose:
                print("pin_mask 2d:", np.sum(pin_mask==0.0), "values in data set shape",
                      pin_mask.shape, "are held fixed")
            # original approach
            if False:
                @numba.njit()
                def pin_mask_constraint(idx,pt):
                    con.freeinf_pt( idx, pt, freeinf_arg )
                fns_idx_pt = [pin_mask_constraint,]
            else: # use freeinf_grad approach instead (maybe a bit faster)
                # implication: user must change BOTH emb and pin_mask of moved points,
                # and then give 'init=emb', 'data_constrain=pin_mask' arguments to continue
                # fitting with (say) 'fit_embed_data'.
                @numba.njit()
                def pin_mask_constraint(idx,pt,grad):
                    return con.freeinf_grad( idx, pt, grad, freeinf_arg )
                fns_idx_grad = [pin_mask_constraint,]

            have_constraints = True
            # OR mirror a tuple-based approach (REMOVED)
            #pin_mask_constraint_tuple = (con.freeinf_pt, freeinf_arg,)
            #@numba.njit()
            #def pin_mask_constraint2(idx,pt):
            #    pin_mask_constraint_tuple[0]( idx, pt, *pin_mask_constraint_tuple[1:] )
            #fns_idx_pt += (pin_mask_constraint2,)

        #assert pin_mask.shape == torch.Size(head_embedding.shape[0]))
        else:
            raise ValueError("pin_mask data_constrain must be a 1 or 2-dim array")

    # call a tuple of jit functions(idx,pt) in sequential order
    #   This did NOT work so well.  Possibly numba issues?
    #print("fns_idx_pt",fns_idx_pt)
    #print("fns_idx_grad",fns_idx_grad)
    # Note: _chain_idx_pt has some numba issues
    #       todo: get rid of list fns_idx_pt etc (not useful)
    wrap_idx_pt = None # or con.noop_pt
    wrap_idx_grad = None # or con.noop_grad
    if len(fns_idx_pt):
        #wrap_idx_pt = _chain_idx_pt( fns_idx_pt )
        if len(fns_idx_pt) > 1:
            print(" Warning: only accepting 1st idx_pt constraint for now")
        wrap_idx_pt = fns_idx_pt[0]
    if len(fns_idx_grad):
        #wrap_idx_grad = _chain_idx_grad( fns_idx_pt )
        if len(fns_idx_grad) > 1:
            print(" Warning: only accepting 1st idx_grad constraint for now")
        wrap_idx_grad = fns_idx_grad[0]

    outconstrain_pt = None
    outconstrain_grad = None
    outconstrain_epoch_pt = None
    outconstrain_final_pt = None
    if output_constrain is not None:
        assert isinstance(output_constrain, dict)
        for k in output_constrain:
            fn = output_constrain[k]
            if k == 'pt': outconstrain_pt = fn; have_constraints=True
            if k == 'grad': outconstrain_grad = fn; have_constraints=True
            if k == 'epoch_pt': outconstrain_epoch_pt = fn; have_constraints=True
            if k == 'final_pt': outconstrain_final_pt = fn; have_constraints=True

    if False and have_constraints:
        if wrap_idx_pt is not None: print("wrap_idx_pt",wrap_idx_pt)
        if wrap_idx_grad is not None: print("wrap_idx_grad",wrap_idx_grad)
        if outconstrain_pt is not None: print("outconstrain_pt",outconstrain_pt)
        if outconstrain_grad is not None: print("outconstrain_grad",outconstrain_grad)
        if outconstrain_epoch_pt is not None: print("outconstrain_epoch_pt",outconstrain_epoch_pt)
        if outconstrain_final_pt is not None: print("outconstrain_final_pt",outconstrain_final_pt)

    #print("euc parallel",parallel)
    #print("euc rng_state",rng_state,type(rng_state))

    #
    # TODO: fix numba errors with parallel=True
    #       There is an issue in numba with 
	# No implementation of function Function(<function runtime_broadcast_assert_shapes at 0x7f0bdd45be50>) found for signature:
	# 
	# >>> runtime_broadcast_assert_shapes(Literal[int](1), array(float64, 1d, C))
	# 
	#There are 2 candidate implementations:
	# - Of which 2 did not match due to:
	# Overload in function 'register_jitable.<locals>.wrap.<locals>.ov_wrap': File: numba/core/extending.py: Line 151.
	#   With argument(s): '(int64, array(float64, 1d, C))':
	#  Rejected as the implementation raised a specific error:
	#    TypeError: missing a required argument: 'arg1'
	#  raised from /home/ml/kruus/anaconda3/envs/miru/lib/python3.9/inspect.py:2977
    #
    #optimize_fn = numba.njit(
    #    _optimize_layout_euclidean_single_epoch, fastmath=True,
    #    #parallel=False,
    #    parallel=parallel,  # <--- Can this be re-enabled?
    #)
    #optimize_fn = _opt_euclidean
    optimize_fn = (opt_euclidean_para if parallel
                   else opt_euclidean)

    if densmap_kwds is None:
        densmap_kwds = {}
    if tqdm_kwds is None:
        tqdm_kwds = {}

    if densmap:
        dens_init_fn = numba.njit(
            _optimize_layout_euclidean_densmap_epoch_init,
            fastmath=True,
            parallel=parallel,
        )

        dens_mu_tot = np.sum(densmap_kwds["mu_sum"]) / 2
        dens_lambda = densmap_kwds["lambda"]
        dens_R = densmap_kwds["R"]
        dens_mu = densmap_kwds["mu"]
        dens_phi_sum = np.zeros(n_vertices, dtype=np.float32)
        dens_re_sum = np.zeros(n_vertices, dtype=np.float32)
        dens_var_shift = densmap_kwds["var_shift"]
    else:
        dens_mu_tot = 0
        dens_lambda = 0
        dens_R = np.zeros(1, dtype=np.float32)
        dens_mu = np.zeros(1, dtype=np.float32)
        dens_phi_sum = np.zeros(1, dtype=np.float32)
        dens_re_sum = np.zeros(1, dtype=np.float32)

    # Note: we do NOT adjust points to initially obey constraints.
    #       'init' conditions are up to the user, and might involve
    #       some best fit of an unconstrained UMAP to the constraints,
    #       followed by manually adjusting constrained points so
    #       they initially obey constraints.
    dens_re_std = 0
    dens_re_mean = 0
    dens_re_cov = 0
    #    for n in range(n_epochs):

    for n in tqdm(range(n_epochs), **tqdm_kwds):

        densmap_flag = (
            densmap
            and (densmap_kwds["lambda"] > 0)
            and (((n + 1) / float(n_epochs)) > (1 - densmap_kwds["frac"]))
        )

        if densmap_flag:
            dens_init_fn(
                head_embedding,
                tail_embedding,
                head,
                tail,
                a,
                b,
                dens_re_sum,
                dens_phi_sum,
            )

            dens_re_std = np.sqrt(np.var(dens_re_sum) + dens_var_shift)
            dens_re_mean = np.mean(dens_re_sum)
            dens_re_cov = np.dot(dens_re_sum, dens_R) / (n_vertices - 1)

        optimize_fn(
            head_embedding,
            tail_embedding,
            head,
            tail,
            n_vertices,
            epochs_per_sample,
            a,
            b,
            rng_state,
            gamma,
            dim,
            move_other,
            alpha,
            epochs_per_negative_sample,
            epoch_of_next_negative_sample,
            epoch_of_next_sample,
            n,
            wrap_idx_pt,        #
            wrap_idx_grad,      #
            outconstrain_pt,    #      constraint fn(pt) or None
            outconstrain_grad,  #
            #outconstrain_epoch_pt = None,
            #outconstrain_final_pt = None,
            densmap_flag,
            dens_phi_sum,
            dens_re_sum,
            dens_re_cov,
            dens_re_std,
            dens_re_mean,
            dens_lambda,
            dens_R,
            dens_mu,
            dens_mu_tot,
        )

        # Should outconstrain_epoch_pt run before epoch loop too?
        if outconstrain_epoch_pt is not None:
            outconstrain_final_pt(head_embedding)
            # not sure move_other ...  XXX
            #if move_other:
            #    outconstrain_final_pt(tail_embedding)
        #    #=======
        #    if pin_mask is None:
        #        pin_mask = {}
        #    have_constraints = False
        #    # historical: packing a list of fns into a single call was problematic
        #    #             perhaps better in numba >= 0.53?
        #    # But eventually it would be nice to accept a list of functions
        #    fns_idx_pt = []
        #    fns_idx_grad = []
        #
        #    if isinstance(pin_mask, dict): # pin_mask is a more generic "constraints" dictionary
        #        # Dictionary layout:
        #        #   key  -->   list( tuple )
        #        #   where tuple ~ (jit_function, [const_args,...]) (or empty)
        #        #print("constraints", pin_mask.keys())
        #        for kk,k in enumerate(pin_mask.keys()):
        #            if verbose: print("kk,k",kk,k)
        #            if k=='idx_pt':
        #                fns_idx_pt = [pin_mask[k]] # revert: do NOT support a list, numba issues?
        #                have_constraints = True
        #            elif k=='idx_grad':
        #                fns_idx_grad = [pin_mask[k]]
        #                have_constraints = True
        #            #elif k=='epoch': fns_epoch = [pin_mask[k][0]]
        #            else:
        #                print(" Warning: unrecognized constraints key", k)
        #                print(" Allowed constraint keys:", recognized)
        #        # I'm trying to avoid numba 0.53 warnings (about firstclass functions)
        #
        #    else:
        #        print("pin_mask", pin_mask.size, pin_mask.shape)
        #        assert (isinstance(pin_mask, np.ndarray))
        #        if len(pin_mask.shape) == 1:
        #            assert pin_mask.shape[0] == head_embedding.shape[0]
        #            # let's use pinindexed_grad for this one
        #            # (it's a more stringent test)
        #            idxs = []
        #            for i in range(pin_mask.size):
        #                if pin_mask[i]==0.0:
        #                    idxs.append(i)
        #            if verbose: print("pin_mask 1d:",  len(idxs), "points don't move")
        #            fixed_pos_idxs = np.array(idxs)
        #            # todo [opt]: no-op if zero points were fixed
        #            @numba.njit()
        #            def pin_mask_1d(idx,pt,grad):
        #                return con.pinindexed_grad(idx,pt,grad, fixed_pos_idxs)
        #            fns_idx_grad = [pin_mask_1d,]
        #            have_constraints = True
        #
        #        elif len(pin_mask.shape) == 2:
        #            assert pin_mask.shape == head_embedding.shape
        #            # DEBUG:
        #            #for i in range(pin_mask.shape[0]):
        #            #    for d in range(dim):
        #            #        if pin_mask[i,d] == 0.0:
        #            #            print("sample",i,"pin head[",d,"] begins at",head_embedding[i,d])
        #            # v.2 translates zeros in pin_mask to embedding values; nonzeros become np.inf
        #            # and then uses a 'freeinf' constraint from constraints.py
        #            # Current fixed point dimensions are copied into the constraint itself.
        #            freeinf_arg = np.where( pin_mask == 0.0, head_embedding, np.float32(np.inf) )
        #            if verbose:
        #                print("pin_mask 2d:", np.sum(pin_mask==0.0), "values in data set shape",
        #                      pin_mask.shape, "are held fixed")
        #            # original approach
        #            if False:
        #                @numba.njit()
        #                def pin_mask_constraint(idx,pt):
        #                    con.freeinf_pt( idx, pt, freeinf_arg )
        #                fns_idx_pt = [pin_mask_constraint,]
        #            else: # use freeinf_grad approach instead (maybe a bit faster)
        #                # implication: user must change BOTH emb and pin_mask of moved points,
        #                # and then give 'init=emb', 'data_constrain=pin_mask' arguments to continue
        #                # fitting with (say) 'fit_embed_data'.
        #                @numba.njit()
        #                def pin_mask_constraint(idx,pt,grad):
        #                    return con.freeinf_grad( idx, pt, grad, freeinf_arg )
        #                fns_idx_grad = [pin_mask_constraint,]
        #
        #            have_constraints = True
        #            # OR mirror a tuple-based approach (REMOVED)
        #            #pin_mask_constraint_tuple = (con.freeinf_pt, freeinf_arg,)
        #            #@numba.njit()
        #            #def pin_mask_constraint2(idx,pt):
        #            #    pin_mask_constraint_tuple[0]( idx, pt, *pin_mask_constraint_tuple[1:] )
        #            #fns_idx_pt += (pin_mask_constraint2,)
        #
        #        #assert pin_mask.shape == torch.Size(head_embedding.shape[0]))
        #        else:
        #            raise ValueError("pin_mask data_constrain must be a 1 or 2-dim array")
        #
        #    # call a tuple of jit functions(idx,pt) in sequential order
        #    #   This did NOT work so well.  Possibly numba issues?
        #    #print("fns_idx_pt",fns_idx_pt)
        #    #print("fns_idx_grad",fns_idx_grad)
        #    # Note: _chain_idx_pt has some numba issues
        #    #       todo: get rid of list fns_idx_pt etc (not useful)
        #    wrap_idx_pt = None # or con.noop_pt
        #    wrap_idx_grad = None # or con.noop_grad
        #    if len(fns_idx_pt):
        #        #wrap_idx_pt = _chain_idx_pt( fns_idx_pt )
        #        if len(fns_idx_pt) > 1:
        #            print(" Warning: only accepting 1st idx_pt constraint for now")
        #        wrap_idx_pt = fns_idx_pt[0]
        #    if len(fns_idx_grad):
        #        #wrap_idx_grad = _chain_idx_grad( fns_idx_pt )
        #        if len(fns_idx_grad) > 1:
        #            print(" Warning: only accepting 1st idx_grad constraint for now")
        #        wrap_idx_grad = fns_idx_grad[0]
        #
        #    outconstrain_pt = None
        #    outconstrain_grad = None
        #    outconstrain_epoch_pt = None
        #    outconstrain_final_pt = None
        #    if output_constrain is not None:
        #        assert isinstance(output_constrain, dict)
        #        for k in output_constrain:
        #            fn = output_constrain[k]
        #            if k == 'pt': outconstrain_pt = fn; have_constraints=True
        #            if k == 'grad': outconstrain_grad = fn; have_constraints=True
        #            if k == 'epoch_pt': outconstrain_epoch_pt = fn; have_constraints=True
        #            if k == 'final_pt': outconstrain_final_pt = fn; have_constraints=True
        #
        #    if False and have_constraints:
        #        if wrap_idx_pt is not None: print("wrap_idx_pt",wrap_idx_pt)
        #        if wrap_idx_grad is not None: print("wrap_idx_grad",wrap_idx_grad)
        #        if outconstrain_pt is not None: print("outconstrain_pt",outconstrain_pt)
        #        if outconstrain_grad is not None: print("outconstrain_grad",outconstrain_grad)
        #        if outconstrain_epoch_pt is not None: print("outconstrain_epoch_pt",outconstrain_epoch_pt)
        #        if outconstrain_final_pt is not None: print("outconstrain_final_pt",outconstrain_final_pt)
        #
        #    #print("euc parallel",parallel)
        #    #print("euc rng_state",rng_state,type(rng_state))
        #
        #    #
        #    # TODO: fix numba errors with parallel=True
        #    #       There is an issue in numba with 
        #	# No implementation of function Function(<function runtime_broadcast_assert_shapes at 0x7f0bdd45be50>) found for signature:
        #	# 
        #	# >>> runtime_broadcast_assert_shapes(Literal[int](1), array(float64, 1d, C))
        #	# 
        #	#There are 2 candidate implementations:
        #	# - Of which 2 did not match due to:
        #	# Overload in function 'register_jitable.<locals>.wrap.<locals>.ov_wrap': File: numba/core/extending.py: Line 151.
        #	#   With argument(s): '(int64, array(float64, 1d, C))':
        #	#  Rejected as the implementation raised a specific error:
        #	#    TypeError: missing a required argument: 'arg1'
        #	#  raised from /home/ml/kruus/anaconda3/envs/miru/lib/python3.9/inspect.py:2977
        #    #
        #    #optimize_fn = numba.njit(
        #    #    _optimize_layout_euclidean_single_epoch, fastmath=True,
        #    #    #parallel=False,
        #    #    parallel=parallel,  # <--- Can this be re-enabled?
        #    #)
        #    #optimize_fn = _opt_euclidean
        #    optimize_fn = (opt_euclidean_para if parallel
        #                   else opt_euclidean)
        #
        #    if densmap_kwds is None:
        #        densmap_kwds = {}
        #    if tqdm_kwds is None:
        #        tqdm_kwds = {}
        #
        #    if densmap:
        #        dens_init_fn = numba.njit(
        #            _optimize_layout_euclidean_densmap_epoch_init,
        #            fastmath=True,
        #            parallel=parallel,
        #        )
        #
        #        dens_mu_tot = np.sum(densmap_kwds["mu_sum"]) / 2
        #        dens_lambda = densmap_kwds["lambda"]
        #        dens_R = densmap_kwds["R"]
        #        dens_mu = densmap_kwds["mu"]
        #        dens_phi_sum = np.zeros(n_vertices, dtype=np.float32)
        #        dens_re_sum = np.zeros(n_vertices, dtype=np.float32)
        #        dens_var_shift = densmap_kwds["var_shift"]
        #    else:
        #        dens_mu_tot = 0
        #        dens_lambda = 0
        #        dens_R = np.zeros(1, dtype=np.float32)
        #        dens_mu = np.zeros(1, dtype=np.float32)
        #        dens_phi_sum = np.zeros(1, dtype=np.float32)
        #        dens_re_sum = np.zeros(1, dtype=np.float32)
        #
        #    if "disable" not in tqdm_kwds:
        #        tqdm_kwds["disable"] = not verbose
        #
        #    for n in tqdm(range(n_epochs), **tqdm_kwds):
        #
        #        densmap_flag = (
        #            densmap
        #            and (densmap_kwds["lambda"] > 0)
        #            and (((n + 1) / float(n_epochs)) > (1 - densmap_kwds["frac"]))
        #        )
        #
        #        if densmap_flag:
        #            # FIXME: dens_init_fn might be referenced before assignment
        #
        #            dens_init_fn(
        #                head_embedding,
        #                tail_embedding,
        #                head,
        #                tail,
        #                a,
        #                b,
        #                dens_re_sum,
        #                dens_phi_sum,
        #            )
        #
        #            # FIXME: dens_var_shift might be referenced before assignment
        #            dens_re_std = np.sqrt(np.var(dens_re_sum) + dens_var_shift)
        #            dens_re_mean = np.mean(dens_re_sum)
        #            dens_re_cov = np.dot(dens_re_sum, dens_R) / (n_vertices - 1)
        #        else:
        #            dens_re_std = 0
        #            dens_re_mean = 0
        #            dens_re_cov = 0
        #
        #        optimize_fn(
        #            head_embedding,
        #            tail_embedding,
        #            head,
        #            tail,
        #            n_vertices,
        #            epochs_per_sample,
        #            a,
        #            b,
        #            rng_state,
        #            gamma,
        #            dim,
        #            move_other,
        #            alpha,
        #            epochs_per_negative_sample,
        #            epoch_of_next_negative_sample,
        #            epoch_of_next_sample,
        #            n,
        #            densmap_flag,
        #            dens_phi_sum,
        #            dens_re_sum,
        #            dens_re_cov,
        #            dens_re_std,
        #            dens_re_mean,
        #            dens_lambda,
        #            dens_R,
        #            dens_mu,
        #            dens_mu_tot,
        #        )
        #
        #        # Should outconstrain_epoch_pt run before epoch loop too?
        #        if outconstrain_epoch_pt is not None:
        #            outconstrain_final_pt(head_embedding)
        #            # not sure move_other ...  XXX
        #            #if move_other:
        #            #    outconstrain_final_pt(tail_embedding)

        alpha = initial_alpha * (1.0 - (float(n) / float(n_epochs)))

        if verbose and n % int(n_epochs / 10) == 0:
            print("\tcompleted ", n, " / ", n_epochs, "epochs")

    if outconstrain_final_pt is not None:
        outconstrain_final_pt(head_embedding)
        #if move_other: # ... probably not
        #    outconstrain_final_pt(tail_embedding)

    return head_embedding


def _optimize_layout_generic_single_epoch(
    epochs_per_sample,
    epoch_of_next_sample,
    head,
    tail,
    head_embedding,
    tail_embedding,
    output_metric,
    output_metric_kwds,
    dim,
    alpha,
    move_other,
    n,
    epoch_of_next_negative_sample,
    epochs_per_negative_sample,
    rng_state,
    n_vertices,
    a,
    b,
    gamma,
):
    for i in range(epochs_per_sample.shape[0]):
        if epoch_of_next_sample[i] <= n:
            j = head[i]
            k = tail[i]

            current = head_embedding[j]
            other = tail_embedding[k]

            dist_output, grad_dist_output = output_metric(
                current, other, *output_metric_kwds
            )
            _, rev_grad_dist_output = output_metric(other, current, *output_metric_kwds)

            if dist_output > 0.0:
                w_l = pow((1 + a * pow(dist_output, 2 * b)), -1)
            else:
                w_l = 1.0
            grad_coeff = 2 * b * (w_l - 1) / (dist_output + 1e-6)

            for d in range(dim):
                grad_d = clip(grad_coeff * grad_dist_output[d])

                current[d] += grad_d * alpha
                if move_other:
                    grad_d = clip(grad_coeff * rev_grad_dist_output[d])
                    other[d] += grad_d * alpha

            epoch_of_next_sample[i] += epochs_per_sample[i]

            n_neg_samples = int(
                (n - epoch_of_next_negative_sample[i]) / epochs_per_negative_sample[i]
            )

            for p in range(n_neg_samples):
                k = tau_rand_int(rng_state) % n_vertices

                other = tail_embedding[k]

                dist_output, grad_dist_output = output_metric(
                    current, other, *output_metric_kwds
                )

                if dist_output > 0.0:
                    w_l = pow((1 + a * pow(dist_output, 2 * b)), -1)
                elif j == k:
                    continue
                else:
                    w_l = 1.0

                grad_coeff = gamma * 2 * b * w_l / (dist_output + 1e-6)

                for d in range(dim):
                    grad_d = clip(grad_coeff * grad_dist_output[d])
                    current[d] += grad_d * alpha

            epoch_of_next_negative_sample[i] += (
                n_neg_samples * epochs_per_negative_sample[i]
            )
    return epoch_of_next_sample, epoch_of_next_negative_sample


def optimize_layout_generic(
    head_embedding,
    tail_embedding,
    head,
    tail,
    n_epochs,
    n_vertices,
    epochs_per_sample,
    a,
    b,
    rng_state,
    gamma=1.0,
    initial_alpha=1.0,
    negative_sample_rate=5.0,
    output_metric=dist.euclidean,
    output_metric_kwds=(),
    verbose=False,
    tqdm_kwds=None,
    move_other=False,
):
    """Improve an embedding using stochastic gradient descent to minimize the
    fuzzy set cross entropy between the 1-skeletons of the high dimensional
    and low dimensional fuzzy simplicial sets. In practice this is done by
    sampling edges based on their membership strength (with the (1-p) terms
    coming from negative sampling similar to word2vec).

    Parameters
    ----------
    head_embedding: array of shape (n_samples, n_components)
        The initial embedding to be improved by SGD.

    tail_embedding: array of shape (source_samples, n_components)
        The reference embedding of embedded points. If not embedding new
        previously unseen points with respect to an existing embedding this
        is simply the head_embedding (again); otherwise it provides the
        existing embedding to embed with respect to.

    head: array of shape (n_1_simplices)
        The indices of the heads of 1-simplices with non-zero membership.

    tail: array of shape (n_1_simplices)
        The indices of the tails of 1-simplices with non-zero membership.

    n_epochs: int
        The number of training epochs to use in optimization.

    n_vertices: int
        The number of vertices (0-simplices) in the dataset.

    epochs_per_sample: array of shape (n_1_simplices)
        A float value of the number of epochs per 1-simplex. 1-simplices with
        weaker membership strength will have more epochs between being sampled.

    a: float
        Parameter of differentiable approximation of right adjoint functor

    b: float
        Parameter of differentiable approximation of right adjoint functor

    rng_state: array of int64, shape (3,)
        The internal state of the rng

    gamma: float (optional, default 1.0)
        Weight to apply to negative samples.

    negative_sample_rate: int (optional, default 5)
        Number of negative samples to use per positive sample.

    verbose: bool (optional, default False)
        Whether to report information on the current progress of the algorithm.

    tqdm_kwds: dict (optional, default None)
        Keyword arguments for tqdm progress bar.

    move_other: bool (optional, default False)
        Whether to adjust tail_embedding alongside head_embedding

    Returns
    -------
    embedding: array of shape (n_samples, n_components)
        The optimized embedding.
    """

    dim = head_embedding.shape[1]
    #move_other = head_embedding.shape[0] == tail_embedding.shape[0]
    # override for parallel debug:
    #if parallel==True:
    #    move_other = False
    #    print("parallel==True --> move_other=False")
    #print("euc parallel",parallel," move_other",move_other)

    alpha = initial_alpha

    epochs_per_negative_sample = epochs_per_sample / negative_sample_rate
    #print("epochs_per_negative_sample[0]",epochs_per_negative_sample[0])
    #print("epochs_per_sample[0]",epochs_per_sample[0])
    epoch_of_next_negative_sample = epochs_per_negative_sample.copy()
    epoch_of_next_sample = epochs_per_sample.copy()

    optimize_fn = numba.njit(
        _optimize_layout_generic_single_epoch,
        fastmath=True,
    )

    if tqdm_kwds is None:
        tqdm_kwds = {}

    if "disable" not in tqdm_kwds:
        tqdm_kwds["disable"] = not verbose

    for n in tqdm(range(n_epochs), **tqdm_kwds):
        optimize_fn(
            epochs_per_sample,
            epoch_of_next_sample,
            head,
            tail,
            head_embedding,
            tail_embedding,
            output_metric,
            output_metric_kwds,
            dim,
            alpha,
            move_other,
            n,
            epoch_of_next_negative_sample,
            epochs_per_negative_sample,
            rng_state,
            n_vertices,
            a,
            b,
            gamma,
        )
        alpha = initial_alpha * (1.0 - (float(n) / float(n_epochs)))

        #if verbose and n % int(n_epochs / 10) == 0:
        #    print("\tcompleted ", n, " / ", n_epochs, "epochs")

    return head_embedding


def _optimize_layout_inverse_single_epoch(
    epochs_per_sample,
    epoch_of_next_sample,
    head,
    tail,
    head_embedding,
    tail_embedding,
    output_metric,
    output_metric_kwds,
    weight,
    sigmas,
    dim,
    alpha,
    move_other,
    n,
    epoch_of_next_negative_sample,
    epochs_per_negative_sample,
    rng_state,
    n_vertices,
    rhos,
    gamma,
):
    for i in range(epochs_per_sample.shape[0]):
        if epoch_of_next_sample[i] <= n:
            j = head[i]
            k = tail[i]

            current = head_embedding[j]
            other = tail_embedding[k]

            dist_output, grad_dist_output = output_metric(
                current, other, *output_metric_kwds
            )

            w_l = weight[i]
            grad_coeff = -(1 / (w_l * sigmas[k] + 1e-6))

            for d in range(dim):
                grad_d = clip(grad_coeff * grad_dist_output[d])

                current[d] += grad_d * alpha
                if move_other:
                    other[d] += -grad_d * alpha

            epoch_of_next_sample[i] += epochs_per_sample[i]

            n_neg_samples = int(
                (n - epoch_of_next_negative_sample[i]) / epochs_per_negative_sample[i]
            )

            for p in range(n_neg_samples):
                k = tau_rand_int(rng_state) % n_vertices

                other = tail_embedding[k]

                dist_output, grad_dist_output = output_metric(
                    current, other, *output_metric_kwds
                )

                # w_l = 0.0 # for negative samples, the edge does not exist
                w_h = np.exp(-max(dist_output - rhos[k], 1e-6) / (sigmas[k] + 1e-6))
                grad_coeff = -gamma * ((0 - w_h) / ((1 - w_h) * sigmas[k] + 1e-6))

                for d in range(dim):
                    grad_d = clip(grad_coeff * grad_dist_output[d])
                    current[d] += grad_d * alpha

            #<<<<<<< HEAD
            #    if outconstrain_final_pt is not None:
            #        outconstrain_final_pt(head_embedding)
            #        #if move_other: # ... probably not
            #        #    outconstrain_final_pt(tail_embedding)
            #
            #    return head_embedding
            #=======
            epoch_of_next_negative_sample[i] += (
                n_neg_samples * epochs_per_negative_sample[i]
            )
            #>>>>>>> 4e72b600515f4078f4ae158e6f715a1ecb3f1e46


def optimize_layout_inverse(
    head_embedding,
    tail_embedding,
    head,
    tail,
    weight,
    sigmas,
    rhos,
    n_epochs,
    n_vertices,
    epochs_per_sample,
    a,
    b,
    rng_state,
    gamma=1.0,
    initial_alpha=1.0,
    negative_sample_rate=5.0,
    output_metric=dist.euclidean,
    output_metric_kwds=(),
    verbose=False,
    tqdm_kwds=None,
    move_other=False,
):
    """Improve an embedding using stochastic gradient descent to minimize the
    fuzzy set cross entropy between the 1-skeletons of the high dimensional
    and low dimensional fuzzy simplicial sets. In practice this is done by
    sampling edges based on their membership strength (with the (1-p) terms
    coming from negative sampling similar to word2vec).

    Parameters
    ----------
    head_embedding: array of shape (n_samples, n_components)
        The initial embedding to be improved by SGD.

    tail_embedding: array of shape (source_samples, n_components)
        The reference embedding of embedded points. If not embedding new
        previously unseen points with respect to an existing embedding this
        is simply the head_embedding (again); otherwise it provides the
        existing embedding to embed with respect to.

    head: array of shape (n_1_simplices)
        The indices of the heads of 1-simplices with non-zero membership.

    tail: array of shape (n_1_simplices)
        The indices of the tails of 1-simplices with non-zero membership.

    weight: array of shape (n_1_simplices)
        The membership weights of the 1-simplices.

    sigmas:

    rhos:

    n_epochs: int
        The number of training epochs to use in optimization.

    n_vertices: int
        The number of vertices (0-simplices) in the dataset.

    epochs_per_sample: array of shape (n_1_simplices)
        A float value of the number of epochs per 1-simplex. 1-simplices with
        weaker membership strength will have more epochs between being sampled.

    a: float
        Parameter of differentiable approximation of right adjoint functor

    b: float
        Parameter of differentiable approximation of right adjoint functor

    rng_state: array of int64, shape (3,)
        The internal state of the rng

    gamma: float (optional, default 1.0)
        Weight to apply to negative samples.

    initial_alpha: float (optional, default 1.0)
        Initial learning rate for the SGD.

    negative_sample_rate: int (optional, default 5)
        Number of negative samples to use per positive sample.

    verbose: bool (optional, default False)
        Whether to report information on the current progress of the algorithm.

    tqdm_kwds: dict (optional, default None)
        Keyword arguments for tqdm progress bar.

    move_other: bool (optional, default False)
        Whether to adjust tail_embedding alongside head_embedding

    Returns
    -------
    embedding: array of shape (n_samples, n_components)
        The optimized embedding.
    """

    dim = head_embedding.shape[1]
    alpha = initial_alpha

    epochs_per_negative_sample = epochs_per_sample / negative_sample_rate
    epoch_of_next_negative_sample = epochs_per_negative_sample.copy()
    epoch_of_next_sample = epochs_per_sample.copy()

    optimize_fn = numba.njit(
        _optimize_layout_inverse_single_epoch,
        fastmath=True,
    )

    if tqdm_kwds is None:
        tqdm_kwds = {}

    if "disable" not in tqdm_kwds:
        tqdm_kwds["disable"] = not verbose

    for n in tqdm(range(n_epochs), **tqdm_kwds):
        optimize_fn(
            epochs_per_sample,
            epoch_of_next_sample,
            head,
            tail,
            head_embedding,
            tail_embedding,
            output_metric,
            output_metric_kwds,
            weight,
            sigmas,
            dim,
            alpha,
            move_other,
            n,
            epoch_of_next_negative_sample,
            epochs_per_negative_sample,
            rng_state,
            n_vertices,
            rhos,
            gamma,
        )
        alpha = initial_alpha * (1.0 - (float(n) / float(n_epochs)))

    #if verbose and n % int(n_epochs / 10) == 0:
    #    print("\tcompleted ", n, " / ", n_epochs, "epochs")
    return head_embedding


def _optimize_layout_aligned_euclidean_single_epoch(
    head_embeddings,
    tail_embeddings,
    heads,
    tails,
    epochs_per_sample,
    a,
    b,
    regularisation_weights,
    relations,
    rng_state,
    gamma,
    lambda_,
    dim,
    move_other,
    alpha,
    epochs_per_negative_sample,
    epoch_of_next_negative_sample,
    epoch_of_next_sample,
    n,
):
    n_embeddings = len(heads)
    window_size = (relations.shape[1] - 1) // 2

    max_n_edges = 0
    for e_p_s in epochs_per_sample:
        if e_p_s.shape[0] >= max_n_edges:
            max_n_edges = e_p_s.shape[0]

    embedding_order = np.arange(n_embeddings).astype(np.int32)
    np.random.seed(abs(rng_state[0]))
    np.random.shuffle(embedding_order)

    for i in range(max_n_edges):
        for m in embedding_order:
            if i < epoch_of_next_sample[m].shape[0] and epoch_of_next_sample[m][i] <= n:
                j = heads[m][i]
                k = tails[m][i]

                current = head_embeddings[m][j]
                other = tail_embeddings[m][k]

                dist_squared = rdist(current, other)

                if dist_squared > 0.0:
                    grad_coeff = -2.0 * a * b * pow(dist_squared, b - 1.0)
                    grad_coeff /= a * pow(dist_squared, b) + 1.0
                else:
                    grad_coeff = 0.0

                for d in range(dim):
                    grad_d = clip(grad_coeff * (current[d] - other[d]))

                    for offset in range(-window_size, window_size):
                        neighbor_m = m + offset
                        #if (
                        #    neighbor_m >= 0
                        #    and neighbor_m < n_embeddings
                        #    and offset != 0
                        #):
                        if n_embeddings > neighbor_m >= 0 != offset:
                            identified_index = relations[m, offset + window_size, j]
                            if identified_index >= 0:
                                grad_d -= clip(
                                    (lambda_ * np.exp(-(np.abs(offset) - 1)))
                                    * regularisation_weights[m, offset + window_size, j]
                                    * (
                                        current[d]
                                        - head_embeddings[neighbor_m][
                                            identified_index, d
                                        ]
                                    )
                                )

                    current[d] += clip(grad_d) * alpha
                    if move_other:
                        other_grad_d = clip(grad_coeff * (other[d] - current[d]))

                        for offset in range(-window_size, window_size):
                            neighbor_m = m + offset
                            if n_embeddings > neighbor_m >= 0 != offset:
                                identified_index = relations[m, offset + window_size, k]
                                if identified_index >= 0:
                                    grad_d -= clip(
                                        (lambda_ * np.exp(-(np.abs(offset) - 1)))
                                        * regularisation_weights[
                                            m, offset + window_size, k
                                        ]
                                        * (
                                            other[d]
                                            - head_embeddings[neighbor_m][
                                                identified_index, d
                                            ]
                                        )
                                    )

                        other[d] += clip(other_grad_d) * alpha

                epoch_of_next_sample[m][i] += epochs_per_sample[m][i]

                if epochs_per_negative_sample[m][i] > 0:
                    n_neg_samples = int(
                        (n - epoch_of_next_negative_sample[m][i])
                        / epochs_per_negative_sample[m][i]
                    )
                else:
                    n_neg_samples = 0

                for p in range(n_neg_samples):
                    k = tau_rand_int(rng_state) % tail_embeddings[m].shape[0]

                    other = tail_embeddings[m][k]

                    dist_squared = rdist(current, other)

                    if dist_squared > 0.0:
                        grad_coeff = 2.0 * gamma * b
                        grad_coeff /= (0.001 + dist_squared) * (
                            a * pow(dist_squared, b) + 1
                        )
                    elif j == k:
                        continue
                    else:
                        grad_coeff = 0.0

                    for d in range(dim):
                        if grad_coeff > 0.0:
                            grad_d = clip(grad_coeff * (current[d] - other[d]))
                        else:
                            grad_d = 4.0

                        for offset in range(-window_size, window_size):
                            neighbor_m = m + offset
                            #if (
                            #    neighbor_m >= 0
                            #    and neighbor_m < n_embeddings
                            #    and offset != 0
                            #):
                            if n_embeddings > neighbor_m >= 0 != offset:
                                identified_index = relations[m, offset + window_size, j]
                                if identified_index >= 0:
                                    grad_d -= clip(
                                        (lambda_ * np.exp(-(np.abs(offset) - 1)))
                                        * regularisation_weights[
                                            m, offset + window_size, j
                                        ]
                                        * (
                                            current[d]
                                            - head_embeddings[neighbor_m][
                                                identified_index, d
                                            ]
                                        )
                                    )

                        current[d] += clip(grad_d) * alpha

                epoch_of_next_negative_sample[m][i] += (
                    n_neg_samples * epochs_per_negative_sample[m][i]
                )

_opt_euclidean_aligned = numba.njit( _optimize_layout_aligned_euclidean_single_epoch,
                                    fastmath=True, parallel=False,)
_opt_euclidean_aligned_para = numba.njit(_optimize_layout_aligned_euclidean_single_epoch,
                                         fastmath=True, parallel=True,)

def optimize_layout_aligned_euclidean(
    head_embeddings,
    tail_embeddings,
    heads,
    tails,
    n_epochs,
    epochs_per_sample,
    regularisation_weights,
    relations,
    rng_state,
    a=1.576943460405378,
    b=0.8950608781227859,
    gamma=1.0,
    lambda_=5e-3,
    initial_alpha=1.0,
    negative_sample_rate=5.0,
    parallel=True,
    verbose=False,
    tqdm_kwds=None,
    move_other=False,
):
    dim = head_embeddings[0].shape[1]
    alpha = initial_alpha

    epochs_per_negative_sample = numba.typed.List.empty_list(numba.types.float32[::1])
    epoch_of_next_negative_sample = numba.typed.List.empty_list(
        numba.types.float32[::1]
    )
    epoch_of_next_sample = numba.typed.List.empty_list(numba.types.float32[::1])

    for m in range(len(heads)):
        epochs_per_negative_sample.append(
            epochs_per_sample[m].astype(np.float32) / negative_sample_rate
        )
        epoch_of_next_negative_sample.append(
            epochs_per_negative_sample[m].astype(np.float32)
        )
        epoch_of_next_sample.append(epochs_per_sample[m].astype(np.float32))

    #optimize_fn = numba.njit(
    #    _optimize_layout_aligned_euclidean_single_epoch,
    #    fastmath=True,
    #    parallel=parallel,
    #)
    optimize_fn = (_opt_euclidean_aligned_para if parallel
                   else _opt_euclidean_aligned)

    if tqdm_kwds is None:
        tqdm_kwds = {}

    if "disable" not in tqdm_kwds:
        tqdm_kwds["disable"] = not verbose

    for n in tqdm(range(n_epochs), **tqdm_kwds):
        optimize_fn(
            head_embeddings,
            tail_embeddings,
            heads,
            tails,
            epochs_per_sample,
            a,
            b,
            regularisation_weights,
            relations,
            rng_state,
            gamma,
            lambda_,
            dim,
            move_other,
            alpha,
            epochs_per_negative_sample,
            epoch_of_next_negative_sample,
            epoch_of_next_sample,
            n,
        )

        alpha = initial_alpha * (1.0 - (float(n) / float(n_epochs)))

    return head_embeddings

@numba.njit(fastmath=True)
def optimize_layout_generic_original(
    head_embedding,
    tail_embedding,
    head,
    tail,
    n_epochs,
    n_vertices,
    epochs_per_sample,
    a,
    b,
    rng_state,
    gamma=1.0,
    initial_alpha=1.0,
    negative_sample_rate=5.0,
    output_metric=dist.euclidean,
    output_metric_kwds=(),
    verbose=False,
    move_other=False,
    tqdm_kwds=None,
):
    """Improve an embedding using stochastic gradient descent to minimize the
    fuzzy set cross entropy between the 1-skeletons of the high dimensional
    and low dimensional fuzzy simplicial sets. In practice this is done by
    sampling edges based on their membership strength (with the (1-p) terms
    coming from negative sampling similar to word2vec).

    Parameters
    ----------
    head_embedding: array of shape (n_samples, n_components)
        The initial embedding to be improved by SGD.

    tail_embedding: array of shape (source_samples, n_components)
        The reference embedding of embedded points. If not embedding new
        previously unseen points with respect to an existing embedding this
        is simply the head_embedding (again); otherwise it provides the
        existing embedding to embed with respect to.

    head: array of shape (n_1_simplices)
        The indices of the heads of 1-simplices with non-zero membership.

    tail: array of shape (n_1_simplices)
        The indices of the tails of 1-simplices with non-zero membership.

    weight: array of shape (n_1_simplices)
        The membership weights of the 1-simplices.

    n_epochs: int
        The number of training epochs to use in optimization.

    n_vertices: int
        The number of vertices (0-simplices) in the dataset.

    epochs_per_sample: array of shape (n_1_simplices)
        A float value of the number of epochs per 1-simplex. 1-simplices with
        weaker membership strength will have more epochs between being sampled.

    a: float
        Parameter of differentiable approximation of right adjoint functor

    b: float
        Parameter of differentiable approximation of right adjoint functor

    rng_state: array of int64, shape (3,)
        The internal state of the rng

    gamma: float (optional, default 1.0)
        Weight to apply to negative samples.

    initial_alpha: float (optional, default 1.0)
        Initial learning rate for the SGD.

    negative_sample_rate: int (optional, default 5)
        Number of negative samples to use per positive sample.

    verbose: bool (optional, default False)
        Whether to report information on the current progress of the algorithm.

    move_other: bool (optional, default False)
        Whether to adjust tail_embedding alongside head_embedding

    tqdm_kwds: dict (optional, default None)
        Keyword arguments for tqdm progress bar.

    Returns
    -------
    embedding: array of shape (n_samples, n_components)
        The optimized embedding.
    """

    dim = head_embedding.shape[1]
    alpha = initial_alpha

    epochs_per_negative_sample = epochs_per_sample / negative_sample_rate
    epoch_of_next_negative_sample = epochs_per_negative_sample.copy()
    epoch_of_next_sample = epochs_per_sample.copy()

    for n in range(n_epochs):
        for i in range(epochs_per_sample.shape[0]):
            if epoch_of_next_sample[i] <= n:
                j = head[i]
                k = tail[i]

                current = head_embedding[j]
                other = tail_embedding[k]

                dist_output, grad_dist_output = output_metric(
                    current, other, *output_metric_kwds
                )
                #_, rev_grad_dist_output = output_metric(
                #    other, current, *output_metric_kwds
                #)

                if dist_output > 0.0:
                    w_l = pow((1 + a * pow(dist_output, 2 * b)), -1)
                else:
                    w_l = 1.0
                grad_coeff = 2 * b * (w_l - 1) / (dist_output + 1e-6)

                for d in range(dim):
                    grad_d = clip(grad_coeff * grad_dist_output[d])

                    current[d] += grad_d * alpha
                    if move_other:
                        #grad_d = clip(grad_coeff * rev_grad_dist_output[d])
                        other[d] += grad_d * alpha

                epoch_of_next_sample[i] += epochs_per_sample[i]

                n_neg_samples = int(
                    (n - epoch_of_next_negative_sample[i])
                    / epochs_per_negative_sample[i]
                )

                for p in range(n_neg_samples):
                    k = tau_rand_int(rng_state) % n_vertices

                    other = tail_embedding[k]

                    dist_output, grad_dist_output = output_metric(
                        current, other, *output_metric_kwds
                    )

                    if dist_output > 0.0:
                        w_l = pow((1 + a * pow(dist_output, 2 * b)), -1)
                    elif j == k:
                        continue
                    else:
                        w_l = 1.0

                    grad_coeff = gamma * 2 * b * w_l / (dist_output + 1e-6)

                    for d in range(dim):
                        grad_d = clip(grad_coeff * grad_dist_output[d])
                        current[d] += grad_d * alpha

                epoch_of_next_negative_sample[i] += (
                    n_neg_samples * epochs_per_negative_sample[i]
                )

        alpha = initial_alpha * (1.0 - (float(n) / float(n_epochs)))

        if verbose and n % int(n_epochs / 10) == 0:
            print("\tcompleted ", n, " / ", n_epochs, "epochs")

    return head_embedding


