import importlib
from copy import deepcopy
from datetime import datetime
from typing import Dict, Union, Optional

import optuna
import numpy as np
import pandas as pd
from sktime.forecasting.compose import TransformedTargetForecaster
from sktime.forecasting.base import ForecastingHorizon, BaseForecaster
from sktime.performance_metrics.forecasting import MeanAbsolutePercentageError
from sktime.forecasting.statsforecast import StatsForecastAutoARIMA as AutoARIMA

from lightwood.helpers.log import log
from lightwood.helpers.templating import _add_cls_kwarg
from lightwood.mixer.base import BaseMixer
from lightwood.api.types import PredictionArguments
from lightwood.data.encoded_ds import EncodedDs, ConcatedEncodedDs


class SkTime(BaseMixer):
    forecaster: str
    horizon: int
    target: str
    supports_proba: bool
    model_path: str
    hyperparam_search: bool

    def __init__(
            self,
            stop_after: float,
            target: str,
            dtype_dict: Dict[str, str],
            horizon: int,
            ts_analysis: Dict,
            model_path: str = None,
            model_kwargs: Optional[dict] = None,
            auto_size: bool = True,
            sp: int = None,
            hyperparam_search: bool = True,
            use_stl: bool = False
    ):
        """
        This mixer is a wrapper around the popular time series library sktime. It exhibits different behavior compared
        to other forecasting mixers, as it predicts based on indices in a forecasting horizon that is defined with
        respect to the last seen data point at training time.
        
        Due to this, the mixer tries to "fit_on_all" so that the latest point in the validation split marks the 
        difference between training data and where forecasts will start. In practice, you need to specify how much 
        time has passed since the aforementioned timestamp for correct forecasts. By default, it is assumed that
         predictions are for the very next timestamp post-training.
        
        If the task has groups (i.e. 'TimeseriesSettings.group_by' is not empty), the mixer will spawn one forecaster 
        object per each different group observed at training time, plus an additional default forecaster fit with all data.
        
        There is an optuna-based automatic hyperparameter search. For now, it considers selecting the forecaster type
        based on the global SMAPE error across all groups.
        
        :param stop_after: time budget in seconds.
        :param target: column to forecast.
        :param dtype_dict: dtypes of all columns in the data.
        :param horizon: length of forecasted horizon.
        :param sp: seasonality period to enforce (instead of automatic inference done at the `ts_analysis` module)
        :param ts_analysis: dictionary with miscellaneous time series info, as generated by 'lightwood.data.timeseries_analyzer'.
        :param model_path: sktime forecaster to use as underlying model(s). Should be a string with format "$module.$class' where '$module' is inside `sktime.forecasting`. Default is 'arima.AutoARIMA'.
        :param model_kwargs: specifies additional paramters to pass to the model if model_path is provided. 
        :param hyperparam_search: bool that indicates whether to perform the hyperparameter tuning or not.
        :param auto_size: whether to filter out old data points if training split is bigger than a certain threshold (defined by the dataset sampling frequency). Enabled by default to avoid long training times in big datasets.
        :param use_stl: Whether to use de-trenders and de-seasonalizers fitted in the timeseries analysis phase.
        """  # noqa
        super().__init__(stop_after)
        self.stable = False
        self.prepared = False
        self.supports_proba = False
        self.target = target
        self.name = 'AutoSKTime'

        default_possible_models = [
            'croston.Croston',
            'theta.ThetaForecaster',
            'trend.STLForecaster',
            'trend.PolynomialTrendForecaster',
        ]

        self.dtype_dict = dtype_dict
        self.ts_analysis = ts_analysis
        self.horizon = horizon
        self.sp = sp
        self.grouped_by = ['__default'] if not ts_analysis['tss'].group_by else ts_analysis['tss'].group_by
        self.auto_size = auto_size
        self.cutoff_factor = 4  # times the detected maximum seasonal period
        self.use_stl = use_stl

        # optuna hyperparameter tuning
        self.models = {}
        self.cutoffs = {}  # last seen timestamp per each model
        self.study = None
        self.hyperparam_dict = {}
        self.model_path = model_path if model_path else 'trend.STLForecaster'
        self.model_kwargs = model_kwargs if model_kwargs else {}
        self.hyperparam_search = hyperparam_search
        self.trial_error_fn = MeanAbsolutePercentageError(symmetric=True)
        self.possible_models = default_possible_models if not model_path else [model_path]
        self.n_trials = len(self.possible_models)
        self.freq = self._get_freq(self.ts_analysis['deltas']['__default'])

        # sktime forecast horizon object is made relative to the end of the latest data point seen at training time
        # the default assumption is to forecast the next `self.horizon` after said data point
        self.fh = ForecastingHorizon(np.arange(1, self.horizon + 1), is_relative=True)

    def fit(self, train_data: EncodedDs, dev_data: EncodedDs) -> None:
        """
        Fits a set of sktime forecasters. The number of models depends on how many groups are observed at training time.

        Forecaster type can be specified by providing the `model_class` argument in `__init__()`. It can also be determined by hyperparameter optimization based on dev data validation error.
        """  # noqa
        log.info(f'Started fitting {self.name} forecaster for array prediction')

        if self.hyperparam_search:
            search_space = {'class': self.possible_models}
            self.study = optuna.create_study(direction='minimize', sampler=optuna.samplers.GridSampler(search_space))
            self.study.optimize(lambda trial: self._get_best_model(trial, train_data, dev_data), n_trials=self.n_trials)
            data = ConcatedEncodedDs([train_data, dev_data])
            self._fit(data)

        else:
            data = ConcatedEncodedDs([train_data, dev_data])
            self._fit(data)

    def _fit(self, data):
        """
        Internal method that fits forecasters to a given dataframe.
        """
        df = data.data_frame.sort_values(by=f'__mdb_original_{self.ts_analysis["tss"].order_by}')
        gby = self.ts_analysis['tss'].group_by

        if not self.hyperparam_search and not self.study:
            module_name = self.model_path
        else:
            finished_study = sum([int(trial.state.is_finished()) for trial in self.study.trials]) == self.n_trials
            if finished_study:
                log.info(f'Selected best model: {self.study.best_params["class"]}')
                module_name = self.study.best_params['class']
            else:
                module_name = self.hyperparam_dict['class']  # select active optuna choice

        sktime_module = importlib.import_module('.'.join(['sktime', 'forecasting', module_name.split(".")[0]]))
        try:
            model_class = getattr(sktime_module, module_name.split(".")[1])
        except AttributeError:
            model_class = AutoARIMA  # use AutoARIMA when the provided class does not exist

        grouped = df.groupby(by=gby) if gby else df.groupby(lambda x: '__default')
        for group, series_data in grouped:
            kwargs = {}
            sp = self.sp if self.sp else self.ts_analysis['periods'].get(group, [1])[0]

            options = self.model_kwargs
            options['sp'] = sp               # seasonality period
            options['suppress_warnings'] = True  # ignore warnings if possible
            options['error_action'] = 'raise'    # avoids fit() failing silently

            if self.model_path == 'fbprophet.Prophet':
                options['freq'] = self.freq

            for k, v in options.items():
                kwargs = _add_cls_kwarg(model_class, kwargs, k, v)

            model_pipeline = []

            if self.use_stl and self.ts_analysis['stl_transforms'].get(group, False):
                model_pipeline.insert(0, ("detrender",
                                          self.ts_analysis['stl_transforms'][group]["transformer"].detrender))
                model_pipeline.insert(0, ("deseasonalizer",
                                          self.ts_analysis['stl_transforms'][group]["transformer"].deseasonalizer))
                kwargs['sp'] = None

            model_pipeline.append(("forecaster", model_class(**kwargs)))

            self.models[group] = TransformedTargetForecaster(model_pipeline)

            oby_col = self.ts_analysis['tss'].order_by
            if self.grouped_by == ['__default']:
                series_oby = df[oby_col]
            else:
                series_oby = series_data[oby_col]

            self.cutoffs[group] = series_oby.index[-1]  # defines the 'present' time for each partition
            series = series_data[self.target]
            if series_data.size > self.ts_analysis['tss'].window:
                series = series.sort_index(ascending=True)
                series = series.reset_index(drop=True)
                series = series.loc[~pd.isnull(series.values)]  # remove NaN  # @TODO: benchmark imputation vs this?

                if self.model_path == 'fbprophet.Prophet':
                    try:
                        series = self._transform_index_to_datetime(series, series_oby, options['freq'])
                    except Exception:
                        if group == '__default':
                            # out of bounds with true freq in __default group is fine, we skip it
                            continue

                series = series.astype(float)

                # if data is huge, filter out old records for quicker fitting
                if self.auto_size:
                    cutoff = min(len(series), max(500, options.get('sp', 1) * self.cutoff_factor))
                    series = series.iloc[-cutoff:]
                try:
                    self.models[group].fit(series, fh=self.fh)
                except Exception:
                    self.models[group] = model_class()  # with default options (i.e. no seasonality, among others)
                    self.models[group].fit(series, fh=self.fh)

    def partial_fit(self, train_data: EncodedDs, dev_data: EncodedDs, args: Optional[dict] = None) -> None:
        """
        Note: sktime asks for "specification of the time points for which forecasts are requested", and this mixer complies by assuming forecasts will start immediately after the last observed value.

        Because of this, `ProblemDefinition.fit_on_all` is set to True so that `partial_fit` uses both `dev` and `test` splits to fit the models.

        Due to how lightwood implements the `update` procedure, expected inputs for this method are:

        :param dev_data: original `test` split (used to validate and select model if ensemble is `BestOf`).
        :param train_data: concatenated original `train` and `dev` splits.
        """  # noqa
        self.hyperparam_search = False
        self.fit(dev_data, train_data)
        self.prepared = True

    def __call__(self, ds: Union[EncodedDs, ConcatedEncodedDs],
                 args: PredictionArguments = PredictionArguments()) -> pd.DataFrame:
        """
        Calls the mixer to emit forecasts.
        
        If there are groups that were not observed at training, a default forecaster (trained on all available data) is used, warning the user that performance might not be optimal.
        
        Latest data point in `train_data` passed to `fit()` determines the starting point for predictions. Relative offsets will be automatically determined when predicting for other starting points.
        """  # noqa
        if args.predict_proba:
            log.warning('This mixer does not output probability estimates')

        df = deepcopy(ds.data_frame)
        df = df.rename_axis('__sktime_index').reset_index()

        gby = self.ts_analysis['tss'].group_by
        ydf = pd.DataFrame(0,  # zero-filled
                           index=df.index,
                           columns=['prediction'],
                           dtype=object)

        pending_idxs = set(df.index)
        grouped = df.groupby(by=gby) if gby else df.groupby(lambda x: '__default')
        for group, series_data in grouped:
            if series_data.size > 0:
                start_ts = series_data['__sktime_index'].iloc[0]
                series = series_data[self.target]
                series_idxs = series.index
                if self.models.get(group, False) and self.models[group].is_fitted:
                    freq = self.ts_analysis['deltas'][group]
                    delta = (start_ts - self.cutoffs[group]).total_seconds()
                    offset = round(delta / freq)
                    forecaster = self.models[group]
                    ydf = self._call_groupmodel(ydf, forecaster, series, offset=offset)
                else:
                    log.warning(f"Applying naive forecaster for novel group {group}. Performance might not be optimal.")
                    ydf = self._call_default(ydf, series.values, series_idxs)
                pending_idxs -= set(series_idxs)

        # apply default model in all remaining novel-group rows
        if len(pending_idxs) > 0:
            series = df[self.target][list(pending_idxs)].squeeze()
            ydf = self._call_default(ydf, series, list(pending_idxs))

        return ydf[['prediction']]

    def _call_groupmodel(self,
                         ydf: pd.DataFrame,
                         model: BaseForecaster,
                         series: pd.Series,
                         offset: int = 0):
        """
        Inner method that calls a `sktime.BaseForecaster`.

        :param offset: indicates relative offset to the latest data point seen during model training. Cannot be less than the number of training data points + the amount of diffences applied internally by the model.
        """  # noqa
        if isinstance(model, TransformedTargetForecaster):
            submodel = model.steps_[-1][-1]
        else:
            submodel = model

        min_offset = -len(submodel._y) + 1
        if hasattr(submodel, 'd'):
            model_d = 0 if submodel.d is None else submodel.d
            min_offset += model_d

        start = max(offset, min_offset)
        end = start + series.shape[0] + self.horizon

        # Workaround for StatsForecastAutoARIMA (see sktime#3600)
        if isinstance(submodel, AutoARIMA):
            all_preds = model.predict(np.arange(min_offset, end)).tolist()[-(end - start):]
        else:
            all_preds = model.predict(np.arange(start, end)).tolist()

        for true_idx, (idx, _) in enumerate(series.items()):
            start_idx = max(0, true_idx)
            end_idx = start_idx + self.horizon
            ydf['prediction'].loc[idx] = all_preds[start_idx:end_idx]
        return ydf

    def _call_default(self, ydf, data, idxs):
        # last value from each window equals shifted target (by 1)  # noqa
        series = np.array([0] + list(data.flatten())[:-1])
        all_preds = [[value for _ in range(self.horizon)] for value in series]
        ydf['prediction'].iloc[idxs] = all_preds
        return ydf

    def _get_best_model(self, trial, train_data, test_data):
        """
        Helper function for Optuna hyperparameter optimization.
        For now, it uses dev data split to find the best model out of the list specified in self.possible_models.
        """

        self.hyperparam_dict = {
            'class': trial.suggest_categorical('class', self.possible_models)
        }
        log.info(f'Starting trial with hyperparameters: {self.hyperparam_dict}')
        try:
            self._fit(train_data)
            y_true = test_data.data_frame[self.target].values[:self.horizon]
            y_pred = pd.DataFrame(self(test_data)['prediction'].iloc[0][:len(y_true)])
            error = self.trial_error_fn(y_true, y_pred)
        except Exception as e:
            log.debug(e)
            error = np.inf

        log.info(f'Trial got error: {error}')
        return error

    def _transform_index_to_datetime(self, series, series_oby, freq):
        series_oby = np.array([np.array(lst) for lst in series_oby])
        start = datetime.utcfromtimestamp(np.min(series_oby[series_oby != np.min(series_oby)]))
        series.index = pd.date_range(start=start, freq=freq, normalize=False, periods=series.shape[0])
        return series

    def _get_freq(self, delta):
        labels = ['S', 'T', 'H', 'D', 'W', 'M', 'Q', 'Y']
        secs = [1,
                60,
                60 * 60,
                60 * 60 * 24,
                60 * 60 * 24 * 7,
                60 * 60 * 24 * 7 * 4,
                60 * 60 * 24 * 7 * 4 * 3,
                60 * 60 * 24 * 7 * 4 * 12]
        min_diff = np.argmin(np.abs(np.array(secs) - delta))
        return labels[min_diff]
