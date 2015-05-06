# -*- coding: utf-8 -*-

"""Clustering utility functions."""

#------------------------------------------------------------------------------
# Imports
#------------------------------------------------------------------------------

import numpy as np

from ...utils.array import _as_array, _index_of
from ...utils._misc import Bunch


#------------------------------------------------------------------------------
# Utility functions
#------------------------------------------------------------------------------

def _unique(x):
    """Faster version of np.unique().

    This version is restricted to 1D arrays of non-negative integers.

    It is only faster if len(x) >> len(unique(x)).

    """
    if len(x) == 0:
        return np.array([], dtype=np.int64)
    return np.nonzero(np.bincount(x))[0]


def _spikes_in_clusters(spike_clusters, clusters):
    """Return the ids of all spikes belonging to the specified clusters."""
    if len(spike_clusters) == 0 or len(clusters) == 0:
        return np.array([], dtype=np.int)
    return np.nonzero(np.in1d(spike_clusters, clusters))[0]


def _spikes_per_cluster(spike_ids, spike_clusters):
    """Return a dictionary {cluster: list_of_spikes}."""
    rel_spikes = np.argsort(spike_clusters)
    abs_spikes = spike_ids[rel_spikes]
    spike_clusters = spike_clusters[rel_spikes]

    diff = np.empty_like(spike_clusters)
    diff[0] = 1
    diff[1:] = np.diff(spike_clusters)

    idx = np.nonzero(diff > 0)[0]
    clusters = spike_clusters[idx]

    spikes_in_clusters = {clusters[i]: np.sort(abs_spikes[idx[i]:idx[i+1]])
                          for i in range(len(clusters) - 1)}
    spikes_in_clusters[clusters[-1]] = np.sort(abs_spikes[idx[-1]:])

    return spikes_in_clusters


def _flatten_spikes_per_cluster(spikes_per_cluster):
    """Convert a dictionary {cluster: list_of_spikes} to a
    spike_clusters array."""
    clusters = sorted(spikes_per_cluster)
    clusters_arr = np.concatenate([(cluster *
                                   np.ones(len(spikes_per_cluster[cluster])))
                                   for cluster in clusters]).astype(np.int64)
    spikes_arr = np.concatenate([spikes_per_cluster[cluster]
                                 for cluster in clusters])
    spike_clusters = np.vstack((spikes_arr, clusters_arr))
    ind = np.argsort(spike_clusters[0, :])
    return spike_clusters[1, ind]


def _concatenate_per_cluster_arrays(spikes_per_cluster, arrays):
    """Concatenate arrays from a {cluster: array} dictionary."""
    assert set(arrays) <= set(spikes_per_cluster)
    clusters = sorted(arrays)
    # Check the sizes of the spikes per cluster and the arrays.
    n_0 = [len(spikes_per_cluster[cluster]) for cluster in clusters]
    n_1 = [len(arrays[cluster]) for cluster in clusters]
    assert n_0 == n_1

    # Concatenate all spikes to find the right insertion order.
    spikes = np.concatenate([spikes_per_cluster[cluster]
                             for cluster in clusters])
    idx = np.argsort(spikes)
    # NOTE: concatenate all arrays along the first axis, because we assume
    # that the first axis represents the spikes.
    arrays = np.concatenate([_as_array(arrays[cluster])
                             for cluster in clusters])
    return arrays[idx, ...]


def _subset_spikes_per_cluster(spikes_per_cluster, arrays, spikes_sub):
    """Cut spikes_per_cluster and arrays along a list of spikes."""
    # WARNING: spikes_sub should be sorted and without duplicates.
    spikes_sub = _as_array(spikes_sub)
    spikes_per_cluster_subset = {}
    arrays_subset = {}
    n = 0
    for cluster in sorted(spikes_per_cluster):
        spikes_c = _as_array(spikes_per_cluster[cluster])
        array = _as_array(arrays[cluster])
        spikes_sc = np.intersect1d(spikes_sub, spikes_c)
        spikes_per_cluster_subset[cluster] = spikes_sc
        idx = _index_of(spikes_sc, spikes_c)
        arrays_subset[cluster] = array[idx, ...]
        assert len(spikes_sc) == len(arrays_subset[cluster])
        n += len(spikes_sc)
    assert n == len(spikes_sub)
    return spikes_per_cluster_subset, arrays_subset


def _update_cluster_selection(clusters, up):
    clusters = list(clusters)
    # Remove deleted clusters.
    clusters = [clu for clu in clusters if clu not in up.deleted]
    # Add new clusters at the end of the selection.
    return clusters + [clu for clu in up.added if clu not in clusters]


#------------------------------------------------------------------------------
# UpdateInfo class
#------------------------------------------------------------------------------

def update_info(**kwargs):
    """Hold information about clustering changes."""
    d = dict(
        description=None,  # information about the update: 'merge', 'assign',
                           # or 'metadata_<name>'
        history=None,  # None, 'undo', or 'redo'
        spikes=[],  # all spikes affected by the update
        added=[],  # new clusters
        deleted=[],  # deleted clusters
        descendants=[],  # pairs of (old_cluster, new_cluster)
        metadata_changed=[],  # clusters with changed metadata
        metadata_value=None,  # new metadata value
        old_spikes_per_cluster={},  # only for the affected clusters
        new_spikes_per_cluster={},  # only for the affected clusters
    )
    d.update(kwargs)
    return Bunch(d)


UpdateInfo = update_info