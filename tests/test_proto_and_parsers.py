"""Unit tests for Google Flow JSPB 0-offset serialization, RPC builders, and parsers."""

from __future__ import annotations

import json

from gflow._proto import (
    RPC_GENERATE_IMAGE,
    RPC_GENERATE_IMAGE_ASYNC,
    RPC_VIDEO_EXPERIMENTAL,
    RPC_VIDEO_EXTEND,
    RPC_VIDEO_REFERENCES,
    RPC_VIDEO_START_END_FRAMES,
    RPC_VIDEO_START_FRAME,
    RPC_VIDEO_TEXT,
    RPC_VIDEO_UPSAMPLE,
    build_create_scene_req,
    build_export_scene_start_req,
    build_generate_image_req,
    build_generate_video_req,
    build_set_scene_clips_req,
    jspb_msg,
    parse_credits,
    parse_generation_response,
    parse_get_scene_response,
    parse_media_item,
    parse_models,
    parse_project_contents,
    parse_project_list,
    resolve_image_aspect,
    resolve_video_aspect,
    resolve_video_model_key,
)
from gflow._rpc import FlowHttpRpc
from gflow.types import SceneClip


def test_jspb_msg_zero_offset_indexing() -> None:
    arr = jspb_msg({1: "alpha", 4: 42})
    assert arr == ["alpha", None, None, 42]


def test_decode_batchexecute_response_handles_anti_xss_prefix() -> None:
    inner = json.dumps([24090, 2, 4, None, 0, 24090, [1796000000]])
    frame = json.dumps([["wrb.fr", "nzlxg", inner, None, None, None, "generic"]])
    wire = ")]}'\n\n" + f"{len(frame)}\n{frame}\n"
    decoded = FlowHttpRpc._parse_batchexecute("nzlxg", wire)
    credits = parse_credits(decoded)
    assert credits.total_credits == 24090
    assert credits.subscription_credits == 24090
    assert credits.renews_at_epoch == 1796000000


def test_generate_image_builder_selects_rpc_and_references() -> None:
    rpc1, req1 = build_generate_image_req(
        "proj-123",
        "cinematic portrait",
        model_key="GEM_PIX_2",
        aspect_ratio="16:9",
        reference_media_ids=["ref-1", "ref-2"],
        recaptcha_token="tok-abc",
        seed=12345,
        async_mode=False,
    )
    assert rpc1 == RPC_GENERATE_IMAGE
    assert req1[3][5] == "proj-123"  # field 4 (ctx) -> field 6 (project_id)
    assert req1[1][0][5] == "GEM_PIX_2"  # field 2 (items[0]) -> field 6 (model_key)
    assert len(req1[1][0][2]) == 2  # field 3 -> two reference images

    rpc2, _ = build_generate_image_req(
        "proj-123",
        "studio shot",
        model_key="NARWHAL",
        aspect_ratio="1:1",
        recaptcha_token="tok-xyz",
        async_mode=True,
    )
    assert rpc2 == RPC_GENERATE_IMAGE_ASYNC


def test_generate_video_builder_dispatches_all_7_modes() -> None:
    # 1. Text-to-Video
    rpc, _, uuids = build_generate_video_req("p1", "ocean waves", model="fast")
    assert rpc == RPC_VIDEO_TEXT
    assert len(uuids) == 1

    # 2. Start-Frame I2V
    rpc, _, _ = build_generate_video_req(
        "p1", "camera zoom", model="quality", start_frame_media_id="img-start"
    )
    assert rpc == RPC_VIDEO_START_FRAME

    # 3. Start + End Frame Interpolation
    rpc, _, _ = build_generate_video_req(
        "p1",
        "smooth morph",
        model="fast",
        start_frame_media_id="img-start",
        end_frame_media_id="img-end",
    )
    assert rpc == RPC_VIDEO_START_END_FRAMES

    # 4. Multi-Reference R2V
    rpc, _, _ = build_generate_video_req(
        "p1",
        "character walks in rain",
        model="fast",
        reference_media_ids=["char-1", "bg-1"],
    )
    assert rpc == RPC_VIDEO_REFERENCES

    # 5. Video Extend
    rpc, _, _ = build_generate_video_req(
        "p1",
        "continue walking",
        extend_video_media_id="vid-prev",
    )
    assert rpc == RPC_VIDEO_EXTEND

    # 6. Video Upsample
    rpc, _, _ = build_generate_video_req(
        "p1",
        "",
        resolution="1080p",
        upsample_video_media_id="vid-720p",
    )
    assert rpc == RPC_VIDEO_UPSAMPLE

    # 7. Omni 1.2 Flash (production RPCs + macaron_cookie_* keys across all modes)
    rpc, req_omni, _ = build_generate_video_req(
        "p1",
        "fast action shot",
        model="omni",
        duration_seconds=6,
        reference_media_ids=["ref-a"],
    )
    assert rpc == RPC_VIDEO_REFERENCES
    assert req_omni[0][0][2] == "macaron_cookie_r2v_6s"
    assert resolve_video_model_key("omni flash1.2", mode="TEXT", duration_seconds=5) == (
        "macaron_cookie_t2v_5s",
        False,
    )
    assert resolve_video_model_key("omni", mode="START_FRAME", duration_seconds=4) == (
        "macaron_cookie_i2v_4s",
        False,
    )
    assert resolve_video_model_key("omni", mode="START_END_FRAMES", duration_seconds=6) == (
        "macaron_cookie_i2v_fl_6s",
        False,
    )
    assert resolve_video_model_key("omni", mode="EXTEND_VIDEO", duration_seconds=5) == (
        "macaron_cookie_extend_5s",
        False,
    )
    # Explicit `abra*` experimental model still dispatches to `IJhj9c`
    rpc_exp, _, _ = build_generate_video_req("p1", "lab test", model="abra_exp_v1")
    assert rpc_exp == RPC_VIDEO_EXPERIMENTAL


def test_scene_builder_requests_and_parsers() -> None:
    create_req = build_create_scene_req(
        "proj-1",
        title="Opening Sequence",
        aspect_ratio="16:9",
        workflow_ids=["wf-1", "wf-2"],
    )
    assert isinstance(create_req, list)

    clips = [
        SceneClip(
            clip_id="c1",
            scene_id="sc-1",
            workflow_id="wf-2",
            primary_media_id="m-2",
            position=0,
            duration_seconds=4.0,
            start_time_seconds=0.5,
            end_time_seconds=3.5,
        )
    ]
    reorder_req = build_set_scene_clips_req("proj-1", "sc-1", clips)
    assert isinstance(reorder_req, list)

    export_req = build_export_scene_start_req(clips)
    # GjQLTd clip entry: field 1 = media_id (idx 0), field 3 = start_pb (idx 2), field 4 = end_pb (idx 3)
    assert export_req[0][0][0] == "m-2"
    assert export_req[0][0][1] is None
    assert export_req[0][0][2] == [0, 500_000_000]
    assert export_req[0][0][3] == [3, 500_000_000]

    # uwAyfb (`RPC_GET_SCENE`) returns `[clip_entries, media_entries]`
    get_scene_raw = [
        [[["wf-2"], "sc-1", [0, [4, 0]]]],
        [["m-2", "proj-1", "wf-2"]],
    ]
    parsed_scene = parse_get_scene_response("proj-1", "sc-1", get_scene_raw, title="Cut 1")
    assert parsed_scene is not None
    assert parsed_scene.title == "Cut 1"
    assert len(parsed_scene.clips) == 1
    assert parsed_scene.clips[0].primary_media_id == "m-2"


def test_parsers_handle_models_projects_and_media() -> None:
    root = jspb_msg(
        {
            5: [
                [
                    "Veo 3.1",
                    [["veo_3_1_t2v_fast_ultra"]],
                    None,
                    "VEO_3_1",
                ]
            ],
            6: [
                [
                    "Nano Banana Pro",
                    [["GEM_PIX_2"]],
                    None,
                    "GEM_PIX_2",
                ]
            ],
        }
    )
    models = parse_models([root])
    assert any(m.key == "veo_3_1_t2v_fast_ultra" and m.kind == "video" for m in models)
    assert any(m.key == "GEM_PIX_2" and m.kind == "image" for m in models)

    raw_projects = [
        [
            ["11111111-2222-3333-4444-555555555555", ["My Film", None, None, None, [1730000000]]],
        ]
    ]
    projects = parse_project_list(raw_projects)
    assert len(projects) == 1
    assert projects[0].title == "My Film"

    contents = parse_project_contents("11111111-2222-3333-4444-555555555555", [[], [], []])
    assert contents.project_id == "11111111-2222-3333-4444-555555555555"

    gen_items = parse_generation_response(
        [
            [
                [
                    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "11111111-2222-3333-4444-555555555555",
                    "https://storage.googleapis.com/demo/video.mp4",
                ]
            ]
        ]
    )
    assert len(gen_items) == 1
    assert gen_items[0].id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_aspect_ratio_wire_encoding_and_video_dimensions() -> None:
    """Regression test: 9:16 portrait vs 16:9 landscape wire enum codes (`dmb` / `_.emb`) & dimensions."""
    # 1. Video aspect ratio enum (`dmb` in Google Flow `js_5.js`): 0=1:1, 1=9:16 (PORTRAIT), 2=16:9 (LANDSCAPE)
    assert resolve_video_aspect("1:1") == (0, False)
    assert resolve_video_aspect("9:16") == (1, True)
    assert resolve_video_aspect("portrait") == (1, True)
    assert resolve_video_aspect("16:9") == (2, False)
    assert resolve_video_aspect("landscape") == (2, False)

    # 2. Image aspect ratio enum (`_.emb` in Google Flow `js_5.js`): 1=1:1, 2=9:16, 3=16:9, 4=3:4, 5=4:3
    assert resolve_image_aspect("1:1") == 1
    assert resolve_image_aspect("9:16") == 2
    assert resolve_image_aspect("16:9") == 3
    assert resolve_image_aspect("3:4") == 4
    assert resolve_image_aspect("4:3") == 5
    _, img_916 = build_generate_image_req("p1", "vertical poster", aspect_ratio="9:16")
    _, img_169 = build_generate_image_req("p1", "wide banner", aspect_ratio="16:9")
    assert img_916[1][0][4] == 2  # field 5 (`aspectRatio`) == 2 (`PORTRAIT`)
    assert img_169[1][0][4] == 3  # field 5 (`aspectRatio`) == 3 (`LANDSCAPE`)

    # 3. Verify 9:16 (`aspect_code == 1`) & 16:9 (`aspect_code == 2`) across ALL 7 video modes
    _, req_t2v_p, _ = build_generate_video_req("p1", "puppy", model="omni", duration_seconds=4, aspect_ratio="9:16")
    _, req_t2v_l, _ = build_generate_video_req("p1", "puppy", model="omni", duration_seconds=4, aspect_ratio="16:9")
    assert req_t2v_p[0][0][1] == "macaron_cookie_t2v_4s"
    assert req_t2v_p[0][0][2] == 1  # field 3 == 1 (PORTRAIT)
    assert req_t2v_l[0][0][2] == 2  # field 3 == 2 (LANDSCAPE)

    _, req_i2v_p, _ = build_generate_video_req(
        "p1", "puppy", model="omni", duration_seconds=4, aspect_ratio="9:16", start_frame_media_id="f1"
    )
    assert req_i2v_p[0][0][1] == "macaron_cookie_i2v_4s"
    assert req_i2v_p[0][0][2] == 1  # field 3 == 1

    _, req_fl_p, _ = build_generate_video_req(
        "p1", "morph", model="fast", aspect_ratio="9:16", start_frame_media_id="f1", end_frame_media_id="f2"
    )
    assert req_fl_p[0][0][1] == "veo_3_1_i2v_s_fast_portrait_ultra_fl"
    assert req_fl_p[0][0][2] == 1  # field 3 == 1

    _, req_r2v_p, _ = build_generate_video_req(
        "p1", "puppy", model="fast", aspect_ratio="9:16", reference_media_ids=["r1"]
    )
    assert req_r2v_p[0][0][2] == "veo_3_1_r2v_fast_portrait_ultra"
    assert req_r2v_p[0][0][3] == 1  # field 4 == 1 (PORTRAIT, NOT 2!)

    _, req_ext_p, _ = build_generate_video_req(
        "p1", "more", model="fast", aspect_ratio="9:16", extend_video_media_id="v1"
    )
    assert req_ext_p[0][0][2] == "veo_3_1_extend_fast_portrait_ultra"
    assert req_ext_p[0][0][3] == 1  # field 4 == 1

    _, req_up_p, _ = build_generate_video_req(
        "p1", "", resolution="1080p", aspect_ratio="9:16", upsample_video_media_id="v1"
    )
    assert req_up_p[0][0][2] == 1  # field 3 == 1

    _, req_exp_p, _ = build_generate_video_req("p1", "lab", model="abra_exp_v1", aspect_ratio="9:16")
    assert req_exp_p[0][0][11] == 1  # field 12 == 1

    # 4. Verify `parse_media_item` populates true pixel width/height for 9:16 vs 16:9 videos
    portrait_entry = jspb_msg(
        {
            1: "8951158e-f061-4c36-b802-6a949962e541",
            2: "1108cc8c-67a7-4273-99d5-263fae124f7a",
            5: jspb_msg({7: [None, [["macaron_cookie_i2v_4s", 2, [1], None, 1, 1]]]}),
            8: [
                jspb_msg(
                    {
                        8: "Shiba Inu",
                        9: "https://flow-content.google/video/8951158e",
                        13: "macaron_cookie_i2v_4s",
                        17: 1,
                    }
                ),
                [None, None, [4]],
            ],
        }
    )
    p_item = parse_media_item(portrait_entry)
    assert p_item is not None
    assert (p_item.width, p_item.height) == (720, 1280)

    landscape_entry = jspb_msg(
        {
            1: "792a9845-6d7d-4788-9430-b32a64e13a61",
            2: "1108cc8c-67a7-4273-99d5-263fae124f7a",
            5: jspb_msg({7: [None, [["macaron_cookie_i2v_4s", 2, [1], None, 2, 1]]]}),
            8: [
                jspb_msg(
                    {
                        8: "Shiba Inu",
                        9: "https://flow-content.google/video/792a9845",
                        13: "macaron_cookie_i2v_4s",
                        17: 2,
                    }
                ),
                [None, None, [4]],
            ],
        }
    )
    l_item = parse_media_item(landscape_entry)
    assert l_item is not None
    assert (l_item.width, l_item.height) == (1280, 720)

