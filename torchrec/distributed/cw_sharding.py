#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from typing import Set, Callable, Dict, List, Optional, Tuple

import torch
import torch.distributed as dist  # noqa
from fbgemm_gpu.permute_pooled_embedding_modules import PermutePooledEmbeddings
from torchrec.distributed.embedding_types import (
    ShardedEmbeddingTable,
    EmbeddingComputeKernel,
)
from torchrec.distributed.tw_sharding import TwEmbeddingSharding, TwPooledEmbeddingDist
from torchrec.distributed.types import (
    ShardingEnv,
    ShardedTensorMetadata,
    ShardMetadata,
    ParameterSharding,
)
from torchrec.modules.embedding_configs import EmbeddingTableConfig


class CwEmbeddingSharding(TwEmbeddingSharding):
    """
    Shards embedding bags column-wise, i.e.. a given embedding table is distributed by
    specified number of columns and table slices are placed on all ranks.
    """

    def __init__(
        self,
        embedding_configs: List[
            Tuple[EmbeddingTableConfig, ParameterSharding, torch.Tensor]
        ],
        env: ShardingEnv,
        device: Optional[torch.device] = None,
        permute_embeddings: bool = False,
    ) -> None:
        super().__init__(
            embedding_configs, env, device, permute_embeddings=permute_embeddings
        )
        if self._permute_embeddings:
            self._init_combined_embeddings()

    def _init_combined_embeddings(self) -> None:
        """
        Grabs the embedding names and dims from TwEmbeddingSharder.

        NOTE:
            This could have duplications if there are multiple shards from the same
            table on a rank. Later on we process these to combine shards together.
        """

        embedding_names: List[str] = super().embedding_names()
        embedding_dims: List[int] = super().embedding_dims()

        embedding_shard_metadata: List[
            Optional[ShardMetadata]
        ] = super().embedding_shard_metadata()

        embedding_name_to_index_offset_tuples: Dict[str, List[Tuple[int, int]]] = {}
        for i, (name, metadata) in enumerate(
            zip(embedding_names, embedding_shard_metadata)
        ):
            if name not in embedding_name_to_index_offset_tuples:
                embedding_name_to_index_offset_tuples[name] = []
            embedding_name_to_index_offset_tuples[name].append(
                (i, metadata.shard_offsets[1] if metadata is not None else 0)
            )

        embedding_name_to_index: Dict[str, List[int]] = {}
        for name, index_offset_tuples in embedding_name_to_index_offset_tuples.items():
            embedding_name_to_index[name] = [
                idx_off_tuple[0]
                for idx_off_tuple in sorted(
                    index_offset_tuples,
                    key=lambda idx_off_tuple: idx_off_tuple[1],
                )
            ]

        combined_embedding_names: List[str] = []
        seen_embedding_names: Set[str] = set()

        for name in embedding_names:
            if name not in seen_embedding_names:
                combined_embedding_names.append(name)
                seen_embedding_names.add(name)

        combined_embedding_dims: List[int] = []

        embedding_order: List[int] = []
        for name in combined_embedding_names:
            combined_embedding_dims.append(
                sum([embedding_dims[idx] for idx in embedding_name_to_index[name]])
            )
            embedding_order.extend(embedding_name_to_index[name])

        self._embedding_names: List[str] = embedding_names
        self._embedding_dims: List[int] = embedding_dims
        self._embedding_order: List[int] = embedding_order

        self._combined_embedding_names: List[str] = combined_embedding_names
        self._combined_embedding_dims: List[int] = combined_embedding_dims

    def _shard(
        self,
        embedding_configs: List[
            Tuple[EmbeddingTableConfig, ParameterSharding, torch.Tensor]
        ],
    ) -> List[List[ShardedEmbeddingTable]]:
        world_size = self._pg.size()
        tables_per_rank: List[List[ShardedEmbeddingTable]] = [
            [] for i in range(world_size)
        ]
        for config in embedding_configs:
            # pyre-fixme [16]
            shards: List[ShardMetadata] = config[1].sharding_spec.shards

            # construct the global sharded_tensor_metadata
            global_metadata = ShardedTensorMetadata(
                shards_metadata=shards,
                size=torch.Size([config[0].num_embeddings, config[0].embedding_dim]),
            )

            # pyre-fixme [6]
            for i, rank in enumerate(config[1].ranks):
                tables_per_rank[rank].append(
                    ShardedEmbeddingTable(
                        num_embeddings=config[0].num_embeddings,
                        embedding_dim=config[0].embedding_dim,
                        name=config[0].name,
                        embedding_names=config[0].embedding_names,
                        data_type=config[0].data_type,
                        feature_names=config[0].feature_names,
                        pooling=config[0].pooling,
                        is_weighted=config[0].is_weighted,
                        has_feature_processor=config[0].has_feature_processor,
                        local_rows=config[0].num_embeddings,
                        local_cols=shards[i].shard_sizes[1],
                        compute_kernel=EmbeddingComputeKernel(config[1].compute_kernel),
                        local_metadata=shards[i],
                        global_metadata=global_metadata,
                    )
                )

        return tables_per_rank

    def embedding_dims(self) -> List[int]:
        return (
            self._combined_embedding_dims
            if self._permute_embeddings
            else super().embedding_dims()
        )

    def embedding_names(self) -> List[str]:
        return (
            self._combined_embedding_names
            if self._permute_embeddings
            else super().embedding_names()
        )

    def create_train_pooled_output_dist(
        self,
        device: Optional[torch.device] = None,
    ) -> TwPooledEmbeddingDist:
        embedding_permute_op: Optional[PermutePooledEmbeddings] = None
        callbacks: Optional[List[Callable[[torch.Tensor], torch.Tensor]]] = None
        if self._permute_embeddings and self._embedding_order != list(
            range(len(self._embedding_order))
        ):
            assert len(self._embedding_order) == len(self._embedding_dims)
            embedding_permute_op = PermutePooledEmbeddings(
                self._embedding_dims,
                self._embedding_order,
            ).to(device=self._device)
            callbacks = [embedding_permute_op]
        return TwPooledEmbeddingDist(
            self._pg, self._dim_sum_per_rank(), self._device, callbacks
        )
