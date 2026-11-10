from api.utils.inputs import get_inputs_from_workflow_api


def test_linked_display_name_is_coerced_to_string():
    wf = {
        "1": {
            "class_type": "ComfyUIDeployExternalText",
            "inputs": {
                "input_id": "prompt",
                "display_name": ["191", 0],
                "description": ["192", 0],
            },
        }
    }
    out = get_inputs_from_workflow_api(wf)
    assert out[0]["display_name"] == ""
    assert out[0]["description"] == ""
    assert out[0]["input_id"] == "prompt"
