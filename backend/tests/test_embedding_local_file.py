"""D453：本地 ONNX 文件夹加载 / 上传的最小验收。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from onnx import TensorProto, helper
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

from backend import config
from backend.services.embedding_service import _LocalOnnxEmbedding, local_file_ready


def _make_tiny_model(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    tokenizer = Tokenizer(WordLevel({"hello": 0, "world": 1, "[UNK]": 2, "[PAD]": 3}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(folder / "tokenizer.json"))

    input_ids = helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "seq"])
    attention_mask = helper.make_tensor_value_info(
        "attention_mask", TensorProto.INT64, ["batch", "seq"]
    )
    output = helper.make_tensor_value_info(
        "last_hidden_state", TensorProto.FLOAT, ["batch", "seq", 4]
    )
    nodes = [
        helper.make_node("Cast", ["input_ids"], ["ids_float"], to=TensorProto.FLOAT),
        helper.make_node("Unsqueeze", ["ids_float", "axes"], ["ids_3d"]),
        helper.make_node("Expand", ["ids_3d", "shape"], ["last_hidden_state"]),
    ]
    axes = helper.make_tensor("axes", TensorProto.INT64, [1], [2])
    shape = helper.make_tensor("shape", TensorProto.INT64, [3], [1, 1, 4])
    graph = helper.make_graph(
        nodes,
        "tiny_embedding",
        [input_ids, attention_mask],
        [output],
        initializer=[axes, shape],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 13
    onnx.save(model, folder / "model.onnx")


def test_local_file_ready_and_embed(tmp_path):
    folder = tmp_path / "local-model"
    _make_tiny_model(folder)
    assert local_file_ready(str(folder)) is True
    assert local_file_ready(str(folder / "model.onnx")) is True

    model = _LocalOnnxEmbedding(str(folder), pooling="mean", max_length=32)
    vectors = model.embed(["hello world", "world"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 4
    assert np.isfinite(vectors).all()


def test_upload_local_model_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODEL_CACHE_DIR", tmp_path / "models")
    from backend.api.embedding import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    files = [
        ("files", ("picked/model.onnx", b"fake-onnx", "application/octet-stream")),
        ("files", ("picked/tokenizer.json", b"{}", "application/json")),
    ]
    response = client.post("/api/embedding/local-model/upload", files=files)
    assert response.status_code == 200, response.text
    body = response.json()
    root = Path(body["path"])
    assert (root / "picked" / "model.onnx").is_file()
    assert (root / "picked" / "tokenizer.json").is_file()
