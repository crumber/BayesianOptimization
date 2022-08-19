import itertools
import numpy as np
import os
import pandas as pd
import warnings
from queue import Queue, Empty

from sklearn.gaussian_process.kernels import Matern
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_percentage_error as mape

from .target_space import TargetSpace
from .event import Events, DEFAULT_EVENTS
from .logger import _get_default_logger
from .util import UtilityFunction, StoppingCriterion, acq_max, ensure_rng


class Observable(object):
    """
    Inspired/Taken from
        https://www.protechtraining.com/blog/post/879#simple-observer
    """

    def __init__(self, events):
        # maps event names to subscribers
        # str -> dict
        self._events = {event: dict() for event in events}

    def get_subscribers(self, event):
        return self._events[event]

    def subscribe(self, event, subscriber, callback=None):
        if callback is None:
            callback = getattr(subscriber, 'update')
        self.get_subscribers(event)[subscriber] = callback

    def unsubscribe(self, event, subscriber):
        del self.get_subscribers(event)[subscriber]

    def dispatch(self, event):
        for _, callback in self.get_subscribers(event).items():
            callback(event, self)


class BayesianOptimization(Observable):
    """
    This class takes the function to optimize as well as the parameters bounds
    in order to find which values for the parameters yield the maximum value
    using Bayesian Optimization.

    Parameters
    ----------
    f: function, optional (default=None)
        Function to be maximized.

    pbounds: dict, optional (default=None)
        Dictionary with parameters names as keys and a tuple with minimum
        and maximum values.
        It is actually a mandatory parameter: if the default value None is left,
        an error will be raised.

    random_state: int or numpy.random.RandomState, optional (default=None)
        If the value is an integer, it is used as the seed for creating a
        `numpy.random.RandomState`. Otherwise the random state provided it is used.
        When set to None, an unseeded random state is generated.

    verbose: int, optional (default=2)
        The level of verbosity.

    bounds_transformer: DomainTransformer, optional (default=None)
        If provided, the transformation is applied to the bounds.

    dataset: str, file handle, or pandas.DataFrame, optional (default=None)
        The dataset specified by the user, or a path/file handle of such file.

    output_path: str, optional (default=None)
        Path to directory in which the results are written. Default value is the working directory.

    target_column: str, optional (default=None)
        Name of the column that will act as the target value of the optimization.
        Only works if dataset is passed.

    debug: bool, optional (default=False)
        Whether or not to print detailed debugging information
    """
    def __init__(self, f=None, pbounds=None, random_state=None, verbose=2, bounds_transformer=None,
                 dataset=None, output_path=None, target_column=None, debug=False):
        # Initialize members from arguments
        self._random_state = ensure_rng(random_state)
        self._verbose = verbose
        self._debug = debug
        self._bounds_transformer = bounds_transformer
        self._output_path = os.getcwd() if output_path is None else os.path.join(output_path)

        # Check for coherence among constructor arguments
        if pbounds is None:
            raise ValueError("pbounds must be specified")
        if f is None and target_column is None:
            raise ValueError("Target column must be specified if no target function f is given")
        if f is not None and target_column is not None:
                raise ValueError("Target column cannot be provided if target function f is also provided")
        if target_column is not None and dataset is None:
            raise ValueError("Dataset must be specified for the given target column")

        # Data structure containing the function to be optimized, the bounds of
        # its domain, and a record of the evaluations we have done so far
        self._space = TargetSpace(target_func=f, pbounds=pbounds, random_state=random_state, dataset=dataset,
                                  target_column=target_column, debug=debug)
        self._queue = Queue()

        if self._bounds_transformer:
            try:
                self._bounds_transformer.initialize(self._space)
            except (AttributeError, TypeError):
                raise TypeError('The transformer must be an instance of '
                                'DomainTransformer')

        # Internal GP regressor
        self._gp = GaussianProcessRegressor(
            kernel=Matern(nu=2.5),
            alpha=1e-6,
            normalize_y=True,
            n_restarts_optimizer=5,
            random_state=self._random_state,
        )

        super(BayesianOptimization, self).__init__(events=DEFAULT_EVENTS)

        if self._debug: print("BayesianOptimization initialization completed")


    @property
    def space(self):
        return self._space

    @property
    def max(self):
        return self._space.max()

    @property
    def res(self):
        return self._space.res()

    @property
    def dataset(self):
        return self._space.dataset


    def register(self, params, target, idx=None):
        """Expect observation with known target"""
        self._space.register(params, target, idx)
        self.dispatch(Events.OPTIMIZATION_STEP)


    def register_optimization_info(self, info_new):
        self._space.register_optimization_info(info_new)


    def probe(self, params, idx=None, lazy=True):
        """
        Evaluates the function on the given points. Useful to guide the optimizer.

        Parameters
        ----------
        params: dict or list
            The parameters where the optimizer will evaluate the function

        idx: int or None, optional (default=None)
            The dataset index of the probed point, or None if no dataset is being used

        lazy: bool, optional (default=True)
            If True, the optimizer will evaluate the points when calling
            maximize(), otherwise it will evaluate it at the moment
        """
        if lazy:
            self._queue.put((idx, params))
        else:
            self._space.probe(params, idx=idx)
            self.dispatch(Events.OPTIMIZATION_STEP)


    def suggest(self, utility_function):
        """
        Get most promising point to probe next

        Parameters
        ----------
        utility_function: UtilityFunction object
            Acquisition function to be maximized

        Returns
        -------
        x_max: dict
            The arg max of the acquisition function

        idx: int or None
            The dataset index of the arg max of the acquisition function, or None if no dataset is being used

        max_acq: float
            The computed maximum of the acquisition function, namely ac(x_max)
        """
        if len(self._space) == 0:
            idx, x_rand = self._space.random_sample()
            return idx, self._space.array_to_params(x_rand)

        # Sklearn's GP throws a large number of warnings at times, but
        # we don't really need to see them here.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._gp.fit(self._space.params, self._space.target)

        if self.dataset is None:
            dataset_acq = None
        else:
            # Flatten memory queue (a list of indexes lists) to one single list
            idxs = list(itertools.chain.from_iterable(self.memory_queue))
            # Create mask to select rows whose index is not included in idxs
            mask = np.ones(self.dataset.shape[0], np.bool)
            mask[idxs] = 0
            # Create dataset to be passed to acq_max()
            dataset_acq = self.dataset.loc[mask, self._space.keys]

        # Find argmax of the acquisition function
        suggestion, idx, acq_val = acq_max(
            ac=utility_function.utility,
            gp=self._gp,
            y_max=self._space.target.max(),
            bounds=self._space.bounds,
            random_state=self._random_state,
            dataset=dataset_acq,
            debug=self._debug
        )

        if self.dataset is not None:
            self.update_memory_queue(self.dataset[self._space.keys], suggestion)

        return self._space.array_to_params(suggestion), idx, acq_val


    def _prime_queue(self, init_points):
        """Make sure there's something in the queue at the very beginning."""
        if self._queue.empty() and self._space.empty:
            init_points = max(init_points, 1)

        if self._debug: print("_prime_queue(): initializing", init_points, "random points")

        for _ in range(init_points):
            idx, x_init = self._space.random_sample()
            self._queue.put((idx, x_init))
            if self.dataset is not None:
                self.update_memory_queue(self.dataset[self._space.keys],
                                         self._space.params_to_array(x_init))


    def _prime_subscriptions(self):
        if not any([len(subs) for subs in self._events.values()]):
            _logger = _get_default_logger(self._verbose)
            self.subscribe(Events.OPTIMIZATION_START, _logger)
            self.subscribe(Events.OPTIMIZATION_STEP, _logger)
            self.subscribe(Events.OPTIMIZATION_END, _logger)


    def maximize(self,
                 init_points=5,
                 n_iter=25,
                 acq='ucb',
                 kappa=2.576,
                 kappa_decay=1,
                 kappa_decay_delay=0,
                 xi=0.0,
                 acq_info={},
                 stop_crit_info={},
                 memory_queue_len=0,
                 **gp_params):
        """
        Probes the target space to find the parameters that yield the maximum
        value for the given function.

        Parameters
        ----------
        init_points: int, optional (default=5)
            Number of iterations before the explorations starts the exploration
            for the maximum.

        n_iter: int, optional (default=25)
            Number of iterations where the method attempts to find the maximum
            value.

        acq: str, optional (default='ucb')
            The acquisition method used. Among others:
                * 'ucb' stands for the Upper Confidence Bounds method
                * 'ei' is the Expected Improvement method
                * 'poi' is the Probability Of Improvement criterion.

        kappa: float, optional (default=2.576)
            Parameter to indicate how closed are the next parameters sampled.
                Higher value = favors spaces that are least explored.
                Lower value = favors spaces where the regression function is the
                highest.

        kappa_decay: float, optional (default=1)
            `kappa` is multiplied by this factor every iteration.

        kappa_decay_delay: int, optional (default=0)
            Number of iterations that must have passed before applying the decay
            to `kappa`.

        xi: float, optional (default=0.0)
            [unused]

        acq_info: dict, optional (default={})
            Information required for using some acquisition functions. Namely:
            * if using Machine Learning models, the 'ml_target' field is the name of the target
              quantity and 'ml_bounds' is a tuple with its lower and upper bounds;
            * if using EIC, it assumes that the target function has the form f(x) = P(x) g(x) + Q(x)
              and is bound to the constraint Gmin <= g(x) <= Gmax. Then, 'eic_bounds' is a tuple with
              Gmin and Gmax, and 'eic_P_func'/'eic_Q_func' are the functions in the definition of f.
              The default values for the latter are P(x) == 1 and Q(x) == 0

        stop_crit_info: dict, optional (default={})
            TODO

        memory_queue_len: int, optional (default=0)
            Length of FIFO memory queue. If used alongside a dataset, at each iteration,
            values which have already been chosen in the last memory_queue_len iterations
            will not be considered
        """
        # Initialize the memory queue, a list of lists of forbidden indexes for the current iteration
        if self._debug: print("Starting maximize()")
        self.memory_queue_len = memory_queue_len
        self.memory_queue = []

        # Initialize other stuff
        self._prime_subscriptions()
        self.dispatch(Events.OPTIMIZATION_START)
        self._prime_queue(init_points)
        self.set_gp_params(**gp_params)

        util = UtilityFunction(kind=acq,
                               kappa=kappa,
                               xi=xi,
                               kappa_decay=kappa_decay,
                               kappa_decay_delay=kappa_decay_delay,
                               acq_info=acq_info,
                               debug=self._debug)
        if self._debug: print("Initializing StoppingCriterion with stop_crit_info = {}".format(stop_crit_info))
        stopcrit = StoppingCriterion(debug=self._debug, **stop_crit_info)
        iteration = 0
        terminated = False

        while not self._queue.empty() or iteration < n_iter:
            # Sample new point from GP
            try:
                idx, x_probe = self._queue.get(block=False)
                acq_val = None
                if self._debug: print("New iteration: selected point from queue, index {}, value {}".format(idx, x_probe))
            except Empty:
                if not stopcrit.hard_stop() and terminated:
                    # Keep the best point found so far
                    x_probe = self.max['params']
                    idx = None
                    acq_info = None
                    if self._debug: print("New iteration: sticking to the best point", x_probe)
                else:
                    if self._debug: print("New iteration {}: suggesting new point".format(iteration))
                    util.update_params()
                    # If requird, train ML model with all space parameters data collected so far
                    if 'ml' in acq:
                        ml_model = self.train_ml_model(y_name=util.ml_target)
                        util.set_ml_model(ml_model)
                    x_probe, idx, acq_val = self.suggest(util)
                    if self._debug: print("Suggested point: index {}, value {}, acquisition {}".format(idx, x_probe, acq_val))
                iteration += 1

            if x_probe is None:
                raise ValueError("No point found")

            # Register new point
            if self.dataset is None or self._space.target_column is None:
                # No dataset, or dataset for X only: we evaluate the target function directly
                if self._debug: print("No dataset, or dataset for X only: evaluating target function")
                self.probe(x_probe, idx=idx, lazy=False)
            else:
                # Dataset for both X and y: register point entirely from dataset without probe()
                if self._debug: print("Dataset Xy: registering dataset point")
                if idx is None:
                    idx, target_value = self._space.find_point_in_dataset(x_probe)
                else:
                    target_value = self.dataset.loc[idx, self._space.target_column]
                self.register(self._space.params_to_array(x_probe), target_value, idx)

            # Register other information about the new point
            other_info = pd.DataFrame(acq_val, columns=['acquisition'], index=[idx])
            if hasattr(util, 'ml_model'):  # register validation MAPE on new point
                ml_target_val = self.get_ml_target_data(util.ml_target).iloc[-1]
                y_true = [ml_target_val]
                y_bar = util.ml_model.predict(pd.DataFrame(x_probe, index=[idx]))
                if self._debug: print("True vs predicted '{}' value: {} vs {}".format(util.ml_target, y_true, y_bar))
                other_info['ml_mape'] = mape(y_true, y_bar)
            else:
                ml_target_val = None
            self.register_optimization_info(other_info)

            if self._debug: print("End of current iteration", 24*"+", sep="\n")


        if self._bounds_transformer and iteration > 0:
            # The bounds transformer should only modify the bounds after the init_points points (only for the true
            # iterations)
            self.set_bounds(
                self._bounds_transformer.transform(self._space))
        print("max:", self.max)
        self.save_res_to_csv()
        self.dispatch(Events.OPTIMIZATION_END)


    def save_res_to_csv(self):
        """Save results of the optimization to csv files located in results directory"""
        os.makedirs(self._output_path, exist_ok=True)
        results = self._space.params
        results['target'] = self._space.target
        results = pd.concat((results, self._space._optimization_info), axis=1)
        results['index'] = results.index.fillna(-1).astype(int)
        results.set_index('index', inplace=True)
        results.to_csv(os.path.join(self._output_path, "results.csv"), index=True)

        print("Results successfully saved to " + self._output_path)


    def set_bounds(self, new_bounds):
        """
        Change the lower and upper searching bounds

        Parameters
        ----------
        new_bounds: dict
            A dictionary with the parameter name and its new bounds
        """
        self._space.set_bounds(new_bounds)


    def set_gp_params(self, **params):
        """Set parameters to the internal Gaussian Process Regressor"""
        self._gp.set_params(**params)


    def train_ml_model(self, y_name):
        """
        Returns the Machine Learning model trained on the current history

        Parameters
        ----------
        y_name: str
            Name of the dataset column that will act as the target of the regression

        Returns
        -------
        model: sklearn.model object
            The trained ML model
        """
        # Build training dataset for the ML model
        X = self._space._params
        if self._debug: print("Dataset for ML model has shape", X.shape)
        y = self.get_ml_target_data(y_name)

        # Initialize and train model
        model = Ridge()
        model.fit(X, y)

        if self._debug:
            try:
                print("Trained ML model:")
                print("Training MAPE =", mape(y, model.predict(X)))
                print("Coefficients =", model.coef_)
            except:
                pass
        return model


    def get_ml_target_data(self, y_name):
        """Fetches target data for ML with the given field/column name"""
        if y_name in self._space._target_dict_info:
            return self._space._target_dict_info[y_name]
        elif self.dataset is None:
            raise KeyError("Target function return values must have '{}' field".format(y_name))
        elif y_name in self.dataset.columns:
            return self.dataset.loc[self._space.indexes, y_name]
        else:
            raise KeyError("Dataset must have '{}' column".format(y_name))


    def update_memory_queue(self, dataset, x_new):
        """
        Updates `self.memory_queue`, the list of dataset entries which cannot be selected
        in the current iteration. The list is always no larger than `memory_queue_len` elements.

        Parameters
        ----------
        dataset: pandas.DataFrame
            The dataset on which to perform filtering

        x_new: numpy.ndarray
            The point which is to be included in the memory queue
        """
        if self.memory_queue_len == 0:
            if self._debug: print("No memory queue to be updated")
            return

        self.memory_queue.append([])
        dataset_vals = dataset.values

        # Place indexes of data equal to x_new in memory queue
        for i in range(dataset_vals.shape[0]):
            if np.array_equal(dataset_vals[i], x_new):
                self.memory_queue[-1].append(i)

        # Remove oldest entry if exceeding max length
        if len(self.memory_queue) > self.memory_queue_len:
            self.memory_queue.pop(0)
            if self._debug: print("Exceeded memory queue length {}, removing first entry".format(self.memory_queue_len))

        if self._debug:
            print("Updated memory queue:", self.memory_queue)
            counts = [len(_) for _ in self.memory_queue]
            print("Counts in memory queue: {} (total: {})".format(counts, sum(counts)))


    def _add_initial_point_dict(self, x_init, idx=None):
        """
        Add one single point as an initial probing point

        Parameters
        ----------
        x_init: dict
            Point to be initialized

        idx: int or None, optional (default=None)
            The dataset index, if any, of the given point
        """
        self.probe(x_init, idx=idx, lazy=True)


    def add_initial_points(self, XX_init, idx=None, ignore_df_index=True):
        """
        Add given point(s) as initial probing points

        Parameters
        ----------
        XX_init: dict, list, tuple, or pandas.DataFrame
            Point (if dict) or list of points (otherwise) to be initialized

        idx: int or None, optional (default=None)
            The dataset index, if any, of the given point. Only used if only one point is given,
            i.e. if `XX_init` is a dict or only has one entry

        ignore_df_index: bool, optional (default=True)
            If XX_init is a `pandas.DataFrame`, whether to use or not its index as a
            collection of true dataset indexes. Use False ONLY if the index of the given
            `DataFrame` is SPECIFICALLY set to match the true dataset indexes, otherwise
            keep True. This parameter is ignored if XX_init is not a `DataFrame`.
        """
        if isinstance(XX_init, dict):
            self._add_initial_point_dict(XX_init, idx)
        elif isinstance(XX_init, (list, tuple)):
            idx = idx if len(XX_init) == 1 else None
            for x in XX_init:
                self._add_initial_point_dict(x, idx)
        elif isinstance(XX_init, pd.DataFrame):
            for i, row in XX_init.iterrows():
                if not ignore_df_index:
                    idx_arg = i
                elif XX_init.shape[0] == 1:
                    idx_arg = idx
                else:
                    idx_arg = None
                self._add_initial_point_dict(row.to_dict(), idx_arg)
        else:
            raise ValueError("Unrecognized type {} in add_initial_points()".format(type(XX_init)))
