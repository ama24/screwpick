"""Uploading, keeping and switching models (no real model is loaded: fakes stand in for them)."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pytest
import yaml

from screwpick.layout import Layout
from screwpick.models import ModelError, ModelStore, find_formats, pick_weights
from screwpick.printer.sim import SimPrinter, SimVision, SimWorld
from screwpick.runner import Runner
from screwpick.transform import Transform2D

CLASSES = ["DIN912_M3", "DIN912_M4", "DIN934_M4"]


def _ncnn(z: zipfile.ZipFile, folder: str, task: str = "segment", names=None) -> None:
    z.writestr(f"{folder}/model.ncnn.param", "7767517\n")
    z.writestr(f"{folder}/model.ncnn.bin", b"\0" * 16)
    z.writestr(f"{folder}/model_ncnn.py", "# ultralytics\n")
    meta = {"task": task, "imgsz": [1024, 1024], "names": dict(enumerate(names or CLASSES))}
    z.writestr(f"{folder}/metadata.yaml", yaml.safe_dump(meta))


def package_zip(tmp: Path, card: dict | None = None, ncnn_names=None) -> Path:
    path = tmp / "run7_package.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("package/best.pt", b"pt")
        _ncnn(z, "package/exports/ncnn_fp16/best_ncnn_model", names=ncnn_names)
        _ncnn(z, "package/exports/ncnn_fp32/best_ncnn_model", names=ncnn_names)
        z.writestr("package/exports/onnx_fp32/best.onnx", b"onnx")
        z.writestr(
            "package/model_card.json",
            json.dumps(
                card
                or {
                    "format": "model-trainer/1",
                    "task": "segment",
                    "classes": CLASSES,
                    "imgsz": 1024,
                    "metrics": {"main_metric": "mask mAP50-95", "main_value": 0.812},
                    "recommended_conf": 0.41,
                }
            ),
        )
    return path


@pytest.fixture
def store(tmp_path: Path) -> ModelStore:
    classes = tmp_path / "classes.yaml"
    classes.write_text(yaml.safe_dump({"names": dict(enumerate([*CLASSES, "DIN125A_M4"]))}))
    return ModelStore(tmp_path / "models", classes, inspect=lambda p: ("pose", ["screw"], 384))


def test_package_upload_picks_ncnn_full_precision(store, tmp_path):
    m = store.add_upload(package_zip(tmp_path), "run7_package.zip")
    assert not (tmp_path / "run7_package.zip").exists()  # the upload is consumed
    assert m.task == "segment" and m.role == "stage1" and m.classes == CLASSES
    assert m.weights == "package/exports/ncnn_fp32/best_ncnn_model"
    assert {f["format"] for f in m.formats} == {"ncnn", "onnx", "pt"} and len(m.formats) == 4
    assert m.metric == "mask mAP50-95" and m.metric_value == 0.812 and m.recommended_conf == 0.41
    assert m.has_card and not m.problems
    assert any("not in this model: DIN125A_M4" in w for w in m.warnings)
    assert store.get(m.id).to_dict() == m.to_dict()
    assert store.weights_path(m).is_dir()


def test_bare_ncnn_folder_detect_model(store, tmp_path):
    path = tmp_path / "best_ncnn_model.zip"
    with zipfile.ZipFile(path, "w") as z:
        _ncnn(z, "best_ncnn_model", task="detect")
    m = store.add_upload(path, path.name, name="old detector")
    assert m.name == "old detector" and m.task == "detect" and m.role == "stage1"
    text = " ".join(m.warnings)
    assert "no outlines" in text and "no model_card.json" in text


def test_single_onnx_uses_the_inspector(store, tmp_path):
    path = tmp_path / "keypoints.onnx"
    path.write_bytes(b"onnx")
    m = store.add_upload(path, path.name)
    assert m.task == "pose" and m.role == "stage2" and m.imgsz == 384
    assert any("no NCNN export" in w for w in m.warnings)


def test_pt_files_are_kept_whole(store, tmp_path):
    """PyTorch .pt files are zip archives inside: they must not be unpacked."""
    path = tmp_path / "best.pt"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("best/data.pkl", b"pickle")
    m = store.add_upload(path, "best.pt")
    assert m.formats == [{"format": "pt", "path": "best.pt"}] and m.weights == "best.pt"
    assert zipfile.is_zipfile(store.weights_path(m))


@pytest.mark.parametrize(
    ("name", "content", "text"),
    [
        ("notes.txt", b"hello", "expected a .zip"),
        ("empty.zip", None, "no model inside"),
        ("evil.zip", "slip", "unsafe path"),
    ],
)
def test_bad_uploads_leave_nothing_behind(store, tmp_path, name, content, text):
    path = tmp_path / name
    if content == "slip":
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("../outside.pt", b"x")
    elif content is None:
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("readme.txt", "nothing")
    else:
        path.write_bytes(content)
    with pytest.raises(ModelError, match=text):
        store.add_upload(path, name)
    assert store.list() == [] and not (tmp_path / "outside.pt").exists()


def test_foreign_model_is_blocked(store, tmp_path):
    path = tmp_path / "coco_ncnn_model.zip"
    with zipfile.ZipFile(path, "w") as z:
        _ncnn(z, "coco_ncnn_model", names=["person", "bicycle"])
    m = store.add_upload(path, path.name)
    assert m.problems == ["none of its classes are in the catalogue: a model of another project?"]


def test_mismatched_package_cannot_be_used(store, tmp_path):
    m = store.add_upload(package_zip(tmp_path, ncnn_names=["A", "B", "C"]), "x.zip")
    assert "different classes" in m.problems[0]
    with pytest.raises(ModelError, match="cannot be used"):
        store.set_active("stage1", m.id)


def test_active_roles_rename_choose_delete(store, tmp_path):
    a = store.add_upload(package_zip(tmp_path), "a.zip")
    kp = tmp_path / "kp.onnx"
    kp.write_bytes(b"x")
    b = store.add_upload(kp, "kp.onnx")
    store.set_active("stage1", a.id)
    store.set_active("stage2", b.id)
    with pytest.raises(ModelError, match="cannot be the stage1"):
        store.set_active("stage1", b.id)
    assert store.active() == {"stage1": a.id, "stage2": b.id, "classifier": None, "head": None}
    store.set_active("head", a.id)  # a stage-1 model can be the head camera's
    with pytest.raises(ModelError, match="cannot be the head"):
        store.set_active("head", b.id)
    store.set_active("head", None)
    with pytest.raises(ModelError, match="in use"):
        store.delete(a.id)
    assert store.rename(a.id, "v3 more nuts").name == "v3 more nuts"
    assert store.choose_weights(a.id, "package/best.pt").weights == "package/best.pt"
    with pytest.raises(ModelError):
        store.choose_weights(a.id, "nope.pt")
    store.set_active("stage2", None)
    store.delete(b.id)
    assert [m.id for m in store.list()] == [a.id]


def test_find_and_pick_weights(tmp_path):
    (tmp_path / "x_ncnn_model").mkdir()
    (tmp_path / "x_ncnn_model" / "model.ncnn.param").write_text("p")
    (tmp_path / "w.pt").write_bytes(b"")
    f = find_formats(tmp_path)
    assert f == [{"format": "pt", "path": "w.pt"}, {"format": "ncnn", "path": "x_ncnn_model"}]
    assert pick_weights(f, ("ncnn", "onnx", "pt")) == "x_ncnn_model"
    assert pick_weights(f, ("pt", "ncnn")) == "w.pt"


def test_active_choice(machine, store, tmp_path):
    from screwpick.vision import active_choice

    a = store.add_upload(package_zip(tmp_path), "a.zip")
    store.set_active("stage1", a.id)
    c = active_choice(machine, store)
    assert c.stage1 == store.weights_path(a) and c.imgsz == 1024 and c.conf == 0.41
    assert c.stage2 is None and c.classifier is None
    machine.models.use_recommended_conf = False
    assert active_choice(machine, store).conf is None


def _runner(machine, store):
    lay = Layout.default()
    world = SimWorld()
    world.populate(lay, n=2)
    vision = SimVision(world, lambda: lay, Transform2D.identity)
    return Runner(machine, SimPrinter(machine, world), vision, store)


def test_web_upload_and_switch(machine, store, tmp_path):
    from screwpick.web.server import make_server

    runner = _runner(machine, store)
    srv = make_server(runner, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body: bytes, ctype="application/json"):
        req = urllib.request.Request(base + path, body, {"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    try:
        data = package_zip(tmp_path).read_bytes()
        res = post("/api/models/upload?filename=run7.zip&name=v1", data, "application/zip")
        mid = res["model"]["id"]
        assert res["model"]["name"] == "v1" and res["active"]["stage1"] == mid  # first: in use
        assert "simulation" in res["loaded"]["error"]
        second = post("/api/models/upload?filename=run8.zip", data, "application/zip")
        assert second["active"]["stage1"] == mid  # later uploads wait for "Use"
        state = post(
            "/api/models/activate",
            json.dumps({"role": "stage1", "id": second["model"]["id"]}).encode(),
        )
        assert state["active"]["stage1"] == second["model"]["id"]
        with urllib.request.urlopen(base + "/api/models", timeout=10) as r:
            assert len(json.loads(r.read())["models"]) == 2
        with pytest.raises(urllib.error.HTTPError) as err:
            post("/api/models/upload?filename=x.txt", b"hello", "text/plain")
        assert err.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as err:
            post("/api/models/test", json.dumps({"id": mid}).encode())
        assert "simulation" in json.loads(err.value.read())["error"]
    finally:
        srv.shutdown()


def test_failed_switch_keeps_the_old_models(machine, store, tmp_path):
    runner = _runner(machine, store)
    a = store.add_upload(package_zip(tmp_path), "a.zip")
    b = store.add_upload(package_zip(tmp_path), "b.zip")
    store.set_active("stage1", a.id)

    def broken(choice):
        raise ValueError("cannot load")

    runner.vision.use_models = broken  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="cannot load"):
        runner.activate_model("stage1", b.id)
    assert store.active()["stage1"] == a.id


class _T:
    def __init__(self, a):
        self.a = np.asarray(a, dtype=float)

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class _Model:
    def __init__(self, boxes=None, obb=None):
        self.res = type("R", (), {"boxes": boxes, "obb": obb, "masks": None})()

    def predict(self, img, **kw):
        self.shape = img.shape
        return [self.res]


def test_run_boxes_detect_and_obb():
    pytest.importorskip("screwsynth")
    from screwpick.analysis import run_boxes

    scene = type("S", (), {"image": np.zeros((2048, 2048, 3), np.uint8)})()
    st = type("St", (), {"stage1_imgsz": 1024, "conf": 0.25, "iou": 0.6, "max_det": 300})()
    boxes = type("B", (), {"xyxy": _T([[10, 20, 30, 25]]), "cls": _T([1]), "conf": _T([0.9])})()
    dets = run_boxes(_Model(boxes=boxes), scene, {1: "DIN912_M4"}, st, "detect")
    assert len(dets) == 1 and dets[0].class_name == "DIN912_M4"
    assert dets[0].polygon.shape == (4, 2)
    assert dets[0].box == pytest.approx((20.5, 40.5, 60.5, 50.5))  # x2 back to full size
    obb = type(
        "O",
        (),
        {"xyxyxyxy": _T([[[0, 0], [10, 0], [10, 4], [0, 4]]]), "cls": _T([0]), "conf": _T([0.8])},
    )()
    dets = run_boxes(_Model(obb=obb), scene, {0: "DIN934_M4"}, st, "obb")
    assert dets[0].class_name == "DIN934_M4" and dets[0].box[2] == pytest.approx(20.5)
    assert run_boxes(_Model(), scene, {}, st, "obb") == []


def test_zip_round_trip_helper():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "x")
    assert zipfile.is_zipfile(io.BytesIO(buf.getvalue()))
