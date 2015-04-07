# -*- coding: utf-8 -*-
from __future__ import print_function

"""Session structure."""

#------------------------------------------------------------------------------
# Imports
#------------------------------------------------------------------------------

import os
import os.path as op
from functools import partial
from collections import defaultdict

import numpy as np

from ...ext.six import string_types
from ...utils._misc import (_phy_user_dir,
                            _ensure_phy_user_dir_exists)
from ...utils.logging import info, warn
from ...utils.event import ProgressReporter
from ...ext.slugify import slugify
from ...utils.event import EventEmitter
from ...io.kwik_model import KwikModel
from ._history import GlobalHistory
from .clustering import Clustering
from ._utils import _spikes_in_clusters
from .selector import Selector
from .store import ClusterStore, StoreItem
from .view_model import (WaveformViewModel,
                         FeatureViewModel,
                         CorrelogramViewModel,
                         )


#------------------------------------------------------------------------------
# BaseSession class
#------------------------------------------------------------------------------

class BaseSession(EventEmitter):
    """Provide actions, views, and an event system for creating an interactive
    session."""
    def __init__(self):
        super(BaseSession, self).__init__()
        self._actions = []

    def action(self, func=None, title=None):
        """Decorator for a callback function of an action.

        The 'title' argument is used as a title for the GUI button.

        """
        if func is None:
            return partial(self.action, title=title)

        # HACK: handle the case where the first argument is the title.
        if isinstance(func, string_types):
            return partial(self.action, title=func)

        # Register the action.
        self._actions.append({'func': func, 'title': title})

        # Set the action function as a Session method.
        setattr(self, func.__name__, func)

        return func

    @property
    def actions(self):
        """List of registered actions."""
        return self._actions

    def execute_action(self, action, *args, **kwargs):
        """Execute an action defined by an item in the 'actions' list."""
        action['func'](*args, **kwargs)


#------------------------------------------------------------------------------
# Store items
#------------------------------------------------------------------------------

class FeatureMasks(StoreItem):
    name = 'features and masks'
    fields = [('features', 'disk'),
              ('masks', 'disk'),
              ('mean_masks', 'memory'),
              ('sum_masks', 'memory'),
              ('n_unmasked_channels', 'memory'),
              ('main_channels', 'memory'),
              ('mean_probe_position', 'memory'),
              ]

    def _prepare_file(self, name, cluster, shape=None, dtype=None):
        """Ensure that a data file exists, is blank, and has the
        right shape.

        Return True if the file needs to be written, False otherwise.

        """
        arr = self.disk_store.load(cluster, name)
        # If the array exists and has the right shape, we assume it's correct.
        if arr is not None and arr.shape == shape:
            # If the first and last lines are empty, something might be wrong.
            if np.all(arr[0] == 0) and np.all(arr[-1] == 0):
                warn("The cluster store for {0:s} ".format(name) +
                     "and cluster {0:d} ".format(cluster) +
                     "is probably corrupted: you should regenerate it.")
            else:
                return False
        # We need to recreate an empty file with the right size here.
        # debug("Creating empty file for {0:s} ".format(name) +
        #       "and cluster {0:d}.".format(cluster))
        self.disk_store.store(cluster, **{name: np.zeros(shape, dtype=dtype)})
        return True

    def _clusters_to_store(self, spikes_per_cluster):
        """Determine whether each cluster needs to be stored or not."""
        # Get the number of spikes per cluster.
        sizes = {cluster: len(spikes)
                 for cluster, spikes in spikes_per_cluster.items()}

        _dtype = {'masks': np.float32, 'features': np.float32}

        n_features = self.model.metadata['nfeatures_per_channel']
        n_channels = self.model.n_channels

        # This dictionary tells whether data must be copied for a
        # given cluster.
        to_store = {}

        # Loop over clusters, prepare the files, and determine which clusters
        # need to be created in the store.
        for cluster, n_spikes in sorted(sizes.items()):
            # Figure out the shape of the cache array for the current cluster.
            _shape = {'masks': (n_spikes, n_channels),
                      'features': (n_spikes, n_channels, n_features)}

            to_store[cluster] = {}
            for name in ('masks', 'features'):
                shape = _shape[name]
                dtype = _dtype[name]
                # Make sure the file exists and has the right shape.
                to_store[cluster][name] = self._prepare_file(name,
                                                             cluster,
                                                             shape=shape,
                                                             dtype=dtype)

        return to_store

    def _store_extra_fields(self, cluster, features, masks):
        # Extra fields.
        sum_masks = masks.sum(axis=0)
        mean_masks = sum_masks / float(masks.shape[0])
        unmasked_channels = np.nonzero(mean_masks > 1e-3)[0]
        n_unmasked_channels = len(unmasked_channels)
        # Weighted mean of the channels, weighted by the mean masks.
        mean_probe_position = (self.model.probe.positions *
                               mean_masks[:, np.newaxis]).mean(axis=0)
        main_channels = np.intersect1d(np.argsort(mean_masks)[::-1],
                                       unmasked_channels)
        self.memory_store.store(cluster,
                                mean_masks=mean_masks,
                                sum_masks=sum_masks,
                                n_unmasked_channels=n_unmasked_channels,
                                mean_probe_position=mean_probe_position,
                                main_channels=main_channels,
                                )

    def store_all_clusters(self, spikes_per_cluster):
        """Initialize all cluster files, loop over all spikes, and
        copy the data."""

        to_store = self._clusters_to_store(spikes_per_cluster)
        n_clusters = len(spikes_per_cluster)

        # Find the list of clusters that need to be stored for either
        # masks or features.
        clusters = [cluster for cluster in sorted(to_store)
                    if (to_store[cluster]['masks'] or
                        to_store[cluster]['features'])]

        # Spikes in the clusters to be stored.
        spikes = _spikes_in_clusters(self.model.spike_clusters, clusters)
        n_spikes = len(spikes)

        # These dictionaries will contain references to the HDF5 arrays
        # from the cluster store, for all clusters that need to be store.
        names = ('masks', 'features')
        files = {name: {} for name in names}
        arrays = {name: {} for name in names}
        cursors = {name: defaultdict(int) for name in names}

        # Initialize the progress reporter.
        pr = self.progress_reporter
        if pr is not None:
            pr.set_max(features_masks=n_spikes,  # loop over all spikes.
                       masks_extra=n_clusters,  # loop over all clusters
                       )

        def _cluster(spike):
            return self.model.spike_clusters[spike]

        def _arr(cluster, name):
            # Open the cluster file in 'a' mode if necessary.
            # We'll close it at the end.
            if cluster not in files[name]:
                files[name][cluster] = self.disk_store.cluster_file(cluster,
                                                                    'a')
            # Load the HDF5 array from the cluster file.
            if cluster not in arrays[name]:
                f = files[name][cluster]
                arrays[name][cluster] = self.disk_store.cluster_array(f, name)
            return arrays[name][cluster]

        fm = self.model.features_masks
        n_features = self.model.metadata['nfeatures_per_channel']
        n_channels = self.model.n_channels

        def _data(name, spike):
            if name == 'features':
                return fm[spike, 0:n_features * n_channels, 0]. \
                    reshape((n_channels, n_features))
            elif name == 'masks':
                return fm[spike, 0:n_features * n_channels:n_features, 1]

        # Loop over all spikes.
        for iteration, spike in enumerate(spikes):

            # Current cluster.
            cluster = _cluster(spike)

            # 'masks' and 'features'.
            for name in names:
                # Store the data if necessary.
                if to_store[cluster][name]:
                    # Get masks or features data.
                    data = _data(name, spike)
                    # Get pointer to the file from the cluster store.
                    arr = _arr(cluster, name)
                    # Append the data
                    i = cursors[name][cluster]
                    arr[i, ...] = data
                    cursors[name][cluster] += 1

            # Update the progress reporter.
            if pr is not None and iteration % 100 == 0:
                pr.increment('features_masks', increment=100)

        # Store all extra fields.
        for cluster in sorted(spikes_per_cluster):
            features = self.disk_store.load(cluster, 'features')
            masks = self.disk_store.load(cluster, 'masks')
            self._store_extra_fields(cluster, features, masks)

            # Update the progress reporter.
            if pr is not None:
                pr.increment('masks_extra')

        # Close all opened HDF5 files.
        for name in names:
            for cluster, f in files[name].items():
                f.close()

        del cursors

    def merge(self, up):
        # TODO
        pass

    def assign(self, up):
        # TODO
        pass


#------------------------------------------------------------------------------
# Session class
#------------------------------------------------------------------------------

def _ensure_disk_store_exists(dir_name, root_path=None):
    # Disk store.
    if root_path is None:
        _ensure_phy_user_dir_exists()
        root_path = _phy_user_dir('cluster_store')
        # Create the disk store if it does not exist.
        if not op.exists(root_path):
            os.mkdir(root_path)
    if not op.exists(root_path):
        raise RuntimeError("Please create the store directory "
                           "{0}".format(root_path))
    # Put the store in a subfolder, using the name.
    dir_name = slugify(dir_name)
    path = op.join(root_path, dir_name)
    if not op.exists(path):
        os.mkdir(path)
    return path


def _process_ups(ups):
    """This function processes the UpdateInfo instances of the two
    undo stacks (clustering and cluster metadata) and concatenates them
    into a single UpdateInfo instance."""
    if len(ups) == 0:
        return
    elif len(ups) == 1:
        return ups[0]
    elif len(ups) == 2:
        up = ups[0]
        up.update(ups[1])
        return up
    else:
        raise NotImplementedError()


class Session(BaseSession):
    """Default manual clustering session.

    Parameters
    ----------
    filename : str
        Path to a .kwik file, to be used if 'model' is not used.
    model : instance of BaseModel
        A Model instance, to be used if 'filename' is not used.

    """
    def __init__(self, store_path=None):
        super(Session, self).__init__()
        self.model = None
        self._store_path = store_path

        # self.action and self.connect are decorators.
        self.action(self.open, title='Open')
        self.action(self.select, title='Select clusters')
        self.action(self.merge, title='Merge')
        self.action(self.split, title='Split')
        self.action(self.move, title='Move clusters to a group')
        self.action(self.undo, title='Undo')
        self.action(self.redo, title='Redo')

        self.connect(self.on_open)
        self.connect(self.on_cluster)

    # Public actions
    # -------------------------------------------------------------------------

    def open(self, filename=None, model=None):
        if model is None:
            model = KwikModel(filename)
        self.model = model
        self.emit('open')

    def select(self, clusters):
        self.selector.selected_clusters = clusters
        self.emit('select', self.selector)

    def merge(self, clusters):
        up = self.clustering.merge(clusters)
        self.emit('cluster', up=up)

    def split(self, spikes):
        up = self.clustering.split(spikes)
        self.emit('cluster', up=up)

    def move(self, clusters, group):
        up = self.cluster_metadata.set_group(clusters, group)
        self.emit('cluster', up=up)

    def undo(self):
        up = self._global_history.undo()
        self.emit('cluster', up=up, add_to_stack=False)

    def redo(self):
        up = self._global_history.redo()
        self.emit('cluster', up=up, add_to_stack=False)

    # Event callbacks
    # -------------------------------------------------------------------------

    def on_open(self):
        """Update the session after new data has been loaded."""
        self._global_history = GlobalHistory(process_ups=_process_ups)
        # TODO: call this after the channel groups has changed.
        # Update the Selector and Clustering instances using the Model.
        spike_clusters = self.model.spike_clusters
        self.clustering = Clustering(spike_clusters)
        self.cluster_metadata = self.model.cluster_metadata
        # TODO: n_spikes_max in a user parameter
        self.selector = Selector(spike_clusters, n_spikes_max=100)

        # Progress reporter.
        self.progress_reporter = ProgressReporter()
        pr = self.progress_reporter

        @pr.connect
        def on_report(value, value_max):
            print("Generating the cluster store: "
                  "{0:.2f}%.".format(100 * value / float(value_max)),
                  end='\r')

        # Kwik store.
        path = _ensure_disk_store_exists(self.model.name,
                                         root_path=self._store_path)
        self.cluster_store = ClusterStore(model=self.model,
                                          path=path,
                                          progress_reporter=pr)
        self.cluster_store.register_item(FeatureMasks)
        # TODO: do not reinitialize the store every time the dataset
        # is loaded! Check if the store exists and check consistency.
        self.cluster_store.generate(self.clustering.spikes_per_cluster)

        @self.connect
        def on_cluster(up=None, add_to_stack=None):
            self.cluster_store.update(up)

    def on_cluster(self, up=None, add_to_stack=True):
        if add_to_stack:
            self._global_history.action(self.clustering)
            # TODO: if metadata
            # self._global_history.action(self.cluster_metadata)

    # Show views
    # -------------------------------------------------------------------------

    def _show_view(self,
                   view_model_class,
                   scale_factor=.01,
                   backend=None,
                   show=True,
                   ):
        view_model = view_model_class(self.model,
                                      store=self.cluster_store,
                                      backend=backend,
                                      scale_factor=scale_factor)
        view = view_model.view

        @self.connect
        def on_open():
            if self.model is None:
                return
            view_model.on_open()
            view.update()

        @self.connect
        def on_cluster(up=None):
            view_model.on_cluster(up)

        @self.connect
        def on_select(selector):
            spikes = selector.selected_spikes
            if len(spikes) == 0:
                return
            if view.visual._empty:
                on_open()
            view_model.on_select(selector.selected_clusters,
                                 selector.selected_spikes)
            view.update()

        # Unregister the callbacks when the view is closed.
        @view.connect
        def on_close(event):
            self.unconnect(on_open, on_cluster, on_select)

        @view.connect
        def on_draw(event):
            if view.visual._empty:
                on_open()
                on_select(self.selector)

        if show:
            view.show()

        return view

    def show_waveforms(self):
        return self._show_view(WaveformViewModel)

    def show_features(self):
        return self._show_view(FeatureViewModel,
                               scale_factor=.01)

    def show_correlograms(self):
        return self._show_view(CorrelogramViewModel)
