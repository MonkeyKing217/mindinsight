# Copyright 2020 Huawei Technologies Co., Ltd.All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Mapper module."""
from ...base import ONNXToMindSporeMapper


class ReLUMapper(ONNXToMindSporeMapper):
    """ReLU mapper."""

    @staticmethod
    def _operation_name_in_ms(*args, **kwargs):
        if not kwargs.get('params'):
            name = "nn.ReLU"
        else:
            params = kwargs['params']
            max_clip = params['max'] if params.get('max') else 0
            min_clip = params['min'] if params.get('min') else 0
            if max_clip == 6 and min_clip == 0:
                name = "nn.ReLU6"
            elif max_clip == min_clip == 0:
                name = "nn.ReLU"
            else:
                name = None
        return name

    @staticmethod
    def _convert_params(**kwargs):
        return dict()

    @staticmethod
    def _convert_trained_weights(**kwargs):
        return dict()

    @staticmethod
    def _convert_settings(**kwargs):
        return dict()
