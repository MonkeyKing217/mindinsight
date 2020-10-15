# Copyright 2019 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""This file is used to define the model lineage python api."""
import numpy as np
import pandas as pd

from mindinsight.lineagemgr.common.exceptions.exceptions import LineageQuerySummaryDataError, \
    LineageQuerierParamException, LineageSearchConditionParamError, LineageParamTypeError, LineageSummaryParseException
from mindinsight.lineagemgr.common.log import logger as log
from mindinsight.lineagemgr.common.validator.model_parameter import SearchModelConditionParameter
from mindinsight.lineagemgr.common.validator.validate import validate_search_model_condition, validate_condition
from mindinsight.lineagemgr.lineage_parser import LineageOrganizer
from mindinsight.lineagemgr.querier.querier import Querier
from mindinsight.optimizer.common.enums import ReasonCode
from mindinsight.optimizer.utils.utils import is_simple_numpy_number
from mindinsight.utils.exceptions import MindInsightException

_METRIC_PREFIX = "[M]"
_USER_DEFINED_PREFIX = "[U]"

USER_DEFINED_INFO_LIMIT = 100


def filter_summary_lineage(data_manager, search_condition=None):
    """
    Filter summary lineage from data_manager or parsing from summaries.

    One of data_manager or summary_base_dir needs to be specified. Support getting
    super_lineage_obj from data_manager or parsing summaries by summary_base_dir.

    Args:
        data_manager (DataManager): Data manager defined as
            mindinsight.datavisual.data_transform.data_manager.DataManager
        search_condition (dict): The search condition.
    """
    search_condition = {} if search_condition is None else search_condition

    try:
        validate_condition(search_condition)
        validate_search_model_condition(SearchModelConditionParameter, search_condition)
    except MindInsightException as error:
        log.error(str(error))
        log.exception(error)
        raise LineageSearchConditionParamError(str(error.message))

    try:
        lineage_objects = LineageOrganizer(data_manager).super_lineage_objs
        result = Querier(lineage_objects).filter_summary_lineage(condition=search_condition)
    except LineageSummaryParseException:
        result = {'object': [], 'count': 0}
    except (LineageQuerierParamException, LineageParamTypeError) as error:
        log.error(str(error))
        log.exception(error)
        raise LineageQuerySummaryDataError("Filter summary lineage failed.")

    return result


def get_flattened_lineage(data_manager, search_condition=None):
    """
    Get lineage data in a table from data manager.

    Args:
        data_manager (mindinsight.datavisual.data_manager.DataManager): An object to manage loading.
        search_condition (dict): The search condition.

    Returns:
        Dict[str, list]: A dict contains keys and values from lineages.

    """
    flatten_dict, user_count = {'train_id': []}, 0
    lineages = filter_summary_lineage(data_manager=data_manager, search_condition=search_condition).get("object", [])
    for index, lineage in enumerate(lineages):
        flatten_dict['train_id'].append(lineage.get("summary_dir"))
        for key, val in _flatten_lineage(lineage.get('model_lineage', {})):
            if key.startswith(_USER_DEFINED_PREFIX) and key not in flatten_dict:
                if user_count > USER_DEFINED_INFO_LIMIT:
                    log.warning("The user_defined_info has reached the limit %s. %r is ignored",
                                USER_DEFINED_INFO_LIMIT, key)
                    continue
                user_count += 1
            if key not in flatten_dict:
                flatten_dict[key] = [None] * index
            flatten_dict[key].append(_parse_value(val))
        for vals in flatten_dict.values():
            if len(vals) == index:
                vals.append(None)
    return flatten_dict


def _flatten_lineage(lineage):
    """Flatten the lineage."""
    for key, val in lineage.items():
        if key == 'metric':
            for k, v in val.items():
                yield f'{_METRIC_PREFIX}{k}', v
        elif key == 'user_defined':
            for k, v in val.items():
                yield f'{_USER_DEFINED_PREFIX}{k}', v
        else:
            yield key, val


def _parse_value(val):
    """Parse value."""
    if isinstance(val, str) and val.lower() in ['nan', 'inf']:
        return np.nan
    return val


class LineageTable:
    """Wrap lineage data in a table."""
    _LOSS_NAME = "loss"
    _NOT_TUNABLE_NAMES = [_LOSS_NAME, "train_id", "device_num", "model_size",
                          "test_dataset_count", "train_dataset_count"]

    def __init__(self, df: pd.DataFrame):
        self._df = df
        self.train_ids = self._df["train_id"].tolist()
        self._drop_columns_info = []
        self._remove_unsupported_columns()

    def _remove_unsupported_columns(self):
        """Remove unsupported columns."""
        columns_to_drop = []
        for name, data in self._df.iteritems():
            if not is_simple_numpy_number(data.dtype):
                columns_to_drop.append(name)

        if columns_to_drop:
            log.debug("Unsupported columns: %s", columns_to_drop)
            self._df = self._df.drop(columns=columns_to_drop)

        for name in columns_to_drop:
            if not name.startswith(_USER_DEFINED_PREFIX):
                continue
            self._drop_columns_info.append({
                "name": name,
                "unselected": True,
                "reason_code": ReasonCode.NOT_ALL_NUMBERS.value
            })

    @property
    def target_names(self):
        """Get names for optimize targets (eg loss, accuracy)."""
        target_names = [name for name in self._df.columns if name.startswith(_METRIC_PREFIX)]
        if self._LOSS_NAME in self._df.columns:
            target_names.append(self._LOSS_NAME)
        return target_names

    @property
    def hyper_param_names(self, tunable=True):
        """Get hyper param names."""
        blocked_names = self._get_blocked_names(tunable)

        hyper_param_names = [
            name for name in self._df.columns
            if not name.startswith(_METRIC_PREFIX) and name not in blocked_names]

        if self._LOSS_NAME in hyper_param_names:
            hyper_param_names.remove(self._LOSS_NAME)

        return hyper_param_names

    def _get_blocked_names(self, tunable):
        if tunable:
            block_names = self._NOT_TUNABLE_NAMES
        else:
            block_names = []
        return block_names

    @property
    def user_defined_hyper_param_names(self):
        """Get user defined hyper param names."""
        names = [name for name in self._df.columns if name.startswith(_USER_DEFINED_PREFIX)]
        return names

    def get_column(self, name):
        """
        Get data for specified column.
        Args:
            name (str): column name.

        Returns:
            np.ndarray, specified column.

        """
        return self._df[name]

    def get_column_values(self, name):
        """
        Get data for specified column.
        Args:
            name (str): column name.

        Returns:
            list, specified column data. If value is np.nan, transform to None.

        """
        return [None if np.isnan(num) else num for num in self._df[name].tolist()]

    @property
    def dataframe_data(self):
        """Get the DataFrame."""
        return self._df

    @property
    def drop_column_info(self):
        """Get dropped columns info."""
        return self._drop_columns_info