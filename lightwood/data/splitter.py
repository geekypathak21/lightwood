from typing import List, Dict
from itertools import product

import numpy as np
import pandas as pd
from type_infer.dtype import dtype

from lightwood.helpers.log import log
from lightwood.api.types import TimeseriesSettings


def splitter(
        data: pd.DataFrame,
        tss: TimeseriesSettings,
        dtype_dict: Dict[str, str],
        seed: int,
        pct_train: float,
        pct_dev: float,
        pct_test: float,
        target: str
) -> Dict[str, pd.DataFrame]:
    """
    Splits data into training, dev and testing datasets. 
    
    The proportion of data for each split must be specified (JSON-AI sets defaults to 80/10/10). First, rows in the dataset are shuffled randomly. Then a simple split is done. If a target value is provided and is of data type categorical/binary, then the splits will be stratified to maintain the representative populations of each class.

    :param data: Input dataset to be split
    :param tss: time-series specific details for splitting
    :param dtype_dict: Dictionary with the data type of all columns
    :param seed: Random state for pandas data-frame shuffling
    :param pct_train: training fraction of data; must be less than 1
    :param pct_dev: dev fraction of data; must be less than 1
    :param pct_test: testing fraction of data; must be less than 1
    :param target: Name of the target column; if specified, data will be stratified on this column

    :returns: A dictionary containing the keys train, test and dev with their respective data frames, as well as the "stratified_on" key indicating which columns the data was stratified on (None if it wasn't stratified on anything)
    """  # noqa
    pct_sum = pct_train + pct_dev + pct_test
    if not (np.isclose(pct_sum, 1, atol=0.001) and np.less(pct_sum, 1 + 1e-5)):
        raise Exception(f'The train, dev and test percentage of the data needs to sum up to 1 (got {pct_sum})')

    # Shuffle the data
    np.random.seed(seed)
    if not tss.is_timeseries:
        data = data.sample(frac=1, random_state=seed).reset_index(drop=True)

    # Check if stratification should be done
    stratify_on = []
    if target is not None:
        if dtype_dict[target] in (dtype.categorical, dtype.binary) and not tss.is_timeseries:
            stratify_on = [target]
        if tss.is_timeseries and isinstance(tss.group_by, list):
            stratify_on = tss.group_by

    # Split the data
    if stratify_on:
        reshuffle = not tss.is_timeseries
        train, dev, test = stratify(data, pct_train, pct_dev, pct_test, stratify_on, seed, reshuffle)
    else:
        train, dev, test = simple_split(data, pct_train, pct_dev, pct_test)

    return {"train": train, "test": test, "dev": dev, "stratified_on": stratify_on}


def simple_split(data: pd.DataFrame,
                 pct_train: float,
                 pct_dev: float,
                 pct_test: float) -> List[pd.DataFrame]:
    """
    Simple split method to separate data into training, dev and testing datasets.

    :param data: Input dataset to be split
    :param pct_train: training fraction of data; must be less than 1
    :param pct_dev: dev fraction of data; must be less than 1
    :param pct_test: testing fraction of data; must be less than 1

    :returns Train, dev, and test dataframes
    """
    train_cutoff = round(data.shape[0] * pct_train)
    dev_cutoff = round(data.shape[0] * pct_dev) + train_cutoff
    test_cutoff = round(data.shape[0] * pct_test) + dev_cutoff

    train = data[:train_cutoff]
    dev = data[train_cutoff:dev_cutoff]
    test = data[dev_cutoff:test_cutoff]

    return [train, dev, test]


def stratify(data: pd.DataFrame,
             pct_train: float,
             pct_dev: float,
             pct_test: float,
             stratify_on: List[str],
             seed: int,
             reshuffle: bool,
             atol: float = 0.05) -> List[pd.DataFrame]:
    """
    Stratified data splitter.

    The `stratify_on` columns yield a cartesian product by which every different subset will be stratified
    independently from the others, and recombined at the end in fractions specified by `pcts`.

    For grouped time series tasks, stratification is done based on the group-by columns.

    :param data: dataframe with data to be split
    :param pct_train: fraction of data to use for training split
    :param pct_dev: fraction of data to use for dev split (used internally by mixers)
    :param pct_test: fraction of data to use for test split (used post-training for analysis)
    :param stratify_on: Columns to consider when stratifying
    :param seed: Random state for pandas data-frame shuffling
    :param reshuffle: specify if reshuffling should be done post-split
    :param atol: absolute tolerance for difference in stratification percentages. If violated, reverts to a non-stratified split.

    :returns Stratified train, dev, test dataframes
    """  # noqa

    train_st = pd.DataFrame(columns=data.columns)
    dev_st = pd.DataFrame(columns=data.columns)
    test_st = pd.DataFrame(columns=data.columns)

    all_group_combinations = list(product(*[data[col].unique() for col in stratify_on]))
    for group in all_group_combinations:
        df = data
        for idx, col in enumerate(stratify_on):
            df = df[df[col] == group[idx]]

        train_cutoff = round(df.shape[0] * pct_train)
        dev_cutoff = round(df.shape[0] * pct_dev) + train_cutoff
        test_cutoff = round(df.shape[0] * pct_test) + dev_cutoff

        train_st = train_st.append(df[:train_cutoff])
        dev_st = dev_st.append(df[train_cutoff:dev_cutoff])
        test_st = test_st.append(df[dev_cutoff:test_cutoff])

    if reshuffle:
        train_st, dev_st, test_st = [df.sample(frac=1, random_state=seed).reset_index(drop=True)
                                     for df in [train_st, dev_st, test_st]]

    # check that stratified lengths conform to expected percentages
    emp_tr = len(train_st) / len(data)
    emp_dev = len(dev_st) / len(data)
    emp_te = len(test_st) / len(data)
    if not np.isclose(emp_tr, pct_train, atol=atol) or \
            not np.isclose(emp_dev, pct_dev, atol=atol) or \
            not np.isclose(emp_te, pct_test, atol=atol):
        log.warning(f"Stratification is outside of imposed tolerance ({atol}) ({emp_tr} train - {emp_dev} dev - {emp_te} test), reverting to a simple split.")  # noqa
        train_st, dev_st, test_st = simple_split(data, pct_train, pct_dev, pct_test)

    return [train_st, dev_st, test_st]
