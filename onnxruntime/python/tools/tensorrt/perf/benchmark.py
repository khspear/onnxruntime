import argparse
import copy
import csv
import json
import logging
import os
import pprint
import re
import sys
import time
import timeit
from datetime import datetime

import coloredlogs
import numpy
import numpy as np
from perf_utils import *

import onnxruntime  # isort:skip
from onnx import numpy_helper  # isort:skip
from float16 import *  # isort:skip
import pandas as pd  # isort:skip

debug = False
sys.path.append(".")
logger = logging.getLogger("")

ep_to_provider_list = {
    cpu: [cpu_ep],
    acl: [acl_ep],
    cuda: [cuda_ep],
    cuda_fp16: [cuda_ep],
    trt: [trt_ep, cuda_ep],
    trt_fp16: [trt_ep, cuda_ep],
}

# latency gain headers
trt_cuda_gain = "TRT_CUDA_gain(%)"
trt_cuda_fp16_gain = "TRT_CUDA_fp16_gain(%)"
trt_native_gain = "TRT_Standalone_gain(%)"
trt_native_fp16_gain = "TRT_Standalone_fp16_gain(%)"

# metadata
FAIL_MODEL_FILE = ".fail_model_map"
LATENCY_FILE = ".latency_map"
METRICS_FILE = ".metrics_map"
SESSION_FILE = ".session_map"
MEMORY_FILE = "./temp_memory.csv"


def split_and_sort_output(string_list):
    string_list = string_list.split("\n")
    string_list.sort()
    return string_list


def is_dynamic(model):
    inp = model.graph.input[0]
    for dim in inp.type.tensor_type.shape.dim:
        if not dim.HasField("dim_value"):
            return True
    return False


def get_model_inputs(model):
    all_inputs = [node.name for node in model.graph.input]
    input_initializers = [node.name for node in model.graph.initializer]
    inputs = list(set(all_inputs) - set(input_initializers))
    return inputs


def run_trt_standalone(trtexec, model_name, model_path, all_inputs_shape, fp16, track_memory):
    logger.info("running standalone trt")
    onnx_model_path = "--onnx=" + model_path

    # load inputs
    input_shape = []
    loaded_inputs = []

    model = onnx.load(model_path)
    ort_inputs = get_model_inputs(model)

    output = get_output(["find", "-L", os.getcwd(), "-name", "test_data*", "-type", "d"])
    test_data_dir = split_and_sort_output(output)[0]

    for i in range(len(ort_inputs)):
        name = ort_inputs[i]
        loaded_input = name + ":" + test_data_dir + "/" + str(i) + ".bin"
        logger.info(loaded_input)
        shape = []
        for j in all_inputs_shape[i]:
            shape.append(str(j))
        shape = "x".join(shape)
        shape = name + ":" + shape
        input_shape.append(shape)
        loaded_inputs.append(loaded_input)

    shapes_arg = "--optShapes=" + ",".join(input_shape)
    inputs_arg = "--loadInputs=" + ",".join(loaded_inputs)
    result = {}
    command = [
        trtexec,
        onnx_model_path,
        "--duration=50",
        "--percentile=90",
        "--workspace=4096",
    ]
    command.extend([inputs_arg])

    # add benchmarking flags
    if is_dynamic(model):
        command.extend([shapes_arg])
    if fp16:
        command.extend(["--fp16"])

    # save engine
    engine_name = model_name + ".engine"
    save_command = command + ["--saveEngine=" + engine_name]
    logger.info(save_command)
    out = get_output(save_command)

    # load engine and inference
    load_command = command + ["--loadEngine=" + engine_name]
    logger.info(load_command)

    mem_usage = None
    p = None
    success = False
    if track_memory:
        p = start_memory_tracking()
        try:
            out = get_output(load_command)
            success = True
            mem_usage = end_memory_tracking(p, success)
        except Exception as e:
            end_memory_tracking(p, success)
            raise (e)
    else:
        out = get_output(load_command)

    # parse trtexec output
    tmp = out.split("\n")
    target_list = []
    for t in tmp:
        if "mean" in t:
            target_list.append(t)

        if "percentile" in t:
            target_list.append(t)

    target = target_list[2]
    avg_latency_match = re.search("mean = (.*?) ms", target)
    if avg_latency_match:
        result["average_latency_ms"] = avg_latency_match.group(1)  # extract number
    percentile_match = re.search("percentile\(90%\) = (.*?) ms", target)
    if percentile_match:
        result["latency_90_percentile"] = percentile_match.group(1)  # extract number
    if mem_usage:
        result["memory"] = mem_usage

    logger.info(result)
    return result


def get_latency_result(runtimes, batch_size):
    latency_ms = sum(runtimes) / float(len(runtimes)) * 1000.0
    latency_variance = numpy.var(runtimes, dtype=numpy.float64) * 1000.0
    throughput = batch_size * (1000.0 / latency_ms)

    result = {
        "test_times": len(runtimes),
        "latency_variance": "{:.2f}".format(latency_variance),
        "latency_90_percentile": "{:.2f}".format(numpy.percentile(runtimes, 90) * 1000.0),
        "latency_95_percentile": "{:.2f}".format(numpy.percentile(runtimes, 95) * 1000.0),
        "latency_99_percentile": "{:.2f}".format(numpy.percentile(runtimes, 99) * 1000.0),
        "average_latency_ms": "{:.2f}".format(latency_ms),
        "QPS": "{:.2f}".format(throughput),
    }
    return result


def get_ort_session_inputs_and_outputs(name, session, ort_input):

    sess_inputs = {}
    sess_outputs = None

    if "bert_squad" in name.lower() or "bert-squad" in name.lower():
        unique_ids_raw_output = ort_input[0]
        input_ids = ort_input[1]
        input_mask = ort_input[2]
        segment_ids = ort_input[3]

        sess_inputs = {
            "unique_ids_raw_output___9:0": unique_ids_raw_output,
            "input_ids:0": input_ids[0:1],
            "input_mask:0": input_mask[0:1],
            "segment_ids:0": segment_ids[0:1],
        }
        sess_outputs = ["unique_ids:0", "unstack:0", "unstack:1"]

    elif "bidaf" in name.lower():
        sess_inputs = {
            "context_word": ort_input[0],
            "context_char": ort_input[2],
            "query_word": ort_input[1],
            "query_char": ort_input[3],
        }
        sess_outputs = ["start_pos", "end_pos"]

    elif "yolov4" in name.lower():
        sess_inputs[session.get_inputs()[0].name] = ort_input[0]
        sess_outputs = ["Identity:0"]

    else:
        sess_inputs = {}
        sess_outputs = []
        for i in range(len(session.get_inputs())):
            sess_inputs[session.get_inputs()[i].name] = ort_input[i]
        for i in range(len(session.get_outputs())):
            sess_outputs.append(session.get_outputs()[i].name)
    return (sess_inputs, sess_outputs)


def track_ep_memory(ep):
    return cpu != ep


def get_trtexec_pid(df, python_pid):
    for pid in df["pid"].tolist():
        if pid != python_pid:
            return pid


def get_max_memory():
    df = pd.read_csv(MEMORY_FILE)
    pid = df["pid"].iloc[0]
    mem_series = df.loc[df["pid"] == pid, " used_gpu_memory [MiB]"]
    max_mem = max(mem_series.str.replace(" MiB", "").astype(int))
    return max_mem


def start_memory_tracking():
    logger.info("starting memory tracking process")
    p = subprocess.Popen(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,used_memory",
            "--format=csv",
            "-l",
            "1",
            "-f",
            MEMORY_FILE,
        ]
    )
    return p


def end_memory_tracking(p, success):
    logger.info("terminating memory tracking process")
    p.terminate()
    p.wait()
    p.kill()
    mem_usage = None
    if success:
        mem_usage = get_max_memory()
    if os.path.exists(MEMORY_FILE):
        os.remove(MEMORY_FILE)
    return mem_usage


def inference_ort_with_ep(ep, session, repeat_times, sess_outputs, sess_inputs, with_binding, io_binding):
    if with_binding and cpu not in ep:  # other eps utilize python binding
        runtime = timeit.repeat(
            lambda: session.run_with_iobinding(io_binding),
            number=1,
            repeat=repeat_times,
        )
    else:
        runtime = timeit.repeat(
            lambda: session.run(sess_outputs, sess_inputs),
            number=1,
            repeat=repeat_times,
        )
    success = True
    return runtime, success


def inference_ort(
    args,
    name,
    session,
    ep,
    ort_inputs,
    result_template,
    repeat_times,
    batch_size,
    track_memory,
):
    runtimes = []
    if args.input_data == "random":
        repeat_times = 1  # warn-up run is included in ort_inputs
    else:
        repeat_times += 1  # add warn-up run

    mem_usages = []
    p = None
    mem_usage = None
    success = False

    # get and load inputs and outputs
    for ort_input in ort_inputs:
        io_binding = session.io_binding()
        sess_inputs, sess_outputs = get_ort_session_inputs_and_outputs(name, session, ort_input)
        if debug:
            logger.info("ORT session inputs:")
            logger.info(sess_inputs)
            logger.info("ORT session outputs:")
            logger.info(sess_outputs)
        if args.io_binding:
            for name, inp in sess_inputs.items():
                io_binding.bind_cpu_input(name, inp)
            for out in sess_outputs:
                io_binding.bind_output(out)

        try:
            if track_memory:
                p = start_memory_tracking()
                runtime, success = inference_ort_with_ep(
                    ep,
                    session,
                    repeat_times,
                    sess_outputs,
                    sess_inputs,
                    args.io_binding,
                    io_binding,
                )
                mem_usage = end_memory_tracking(p, success)
                mem_usages.append(mem_usage)
            else:
                runtime, success = inference_ort_with_ep(
                    ep,
                    session,
                    repeat_times,
                    sess_outputs,
                    sess_inputs,
                    args.io_binding,
                    io_binding,
                )
            if args.input_data == "fix":
                runtime = runtime[1:]  # remove warmup
            runtimes += runtime

        except Exception as e:
            logger.error(e)
            if track_memory:
                end_memory_tracking(p, success)
            raise (e)

    if len(mem_usages) > 0:
        mem_usage = max(mem_usages)

    result = {}
    result.update(result_template)
    result.update({"io_binding": True})
    latency_result = get_latency_result(runtimes, batch_size)
    result.update(latency_result)
    return result, mem_usage


def inference_ort_and_get_prediction(name, session, ort_inputs):

    ort_outputs = []
    for ort_input in ort_inputs:
        sess_inputs, sess_outputs = get_ort_session_inputs_and_outputs(name, session, ort_input)
        if debug:
            logger.info("ORT session inputs:")
            logger.info(sess_inputs)
            logger.info("ORT session outputs:")
            logger.info(sess_outputs)

        result = session.run(sess_outputs, sess_inputs)

        if debug:
            logger.info("ORT session output results:")
            logger.info(result)

        # handle shape of output differently
        if "bert_squad" in name.lower():
            ort_outputs.append([result])
        elif "shufflenet-v2" in name.lower() or "shufflenet_v2" in name.lower():
            ort_outputs.append(result[0])
        else:
            ort_outputs.append(result)

    return ort_outputs


def get_acl_version():
    from pathlib import Path

    home = str(Path.home())
    p = subprocess.run(["find", home, "-name", "libarm_compute.so"], check=True, stdout=subprocess.PIPE)
    libarm_compute_path = p.stdout.decode("ascii").strip()
    if libarm_compute_path == "":
        return "No Compute Library Found"
    else:
        p = subprocess.run(["strings", libarm_compute_path], check=True, stdout=subprocess.PIPE)
        libarm_so_strings = p.stdout.decode("ascii").strip()
        version_match = re.search(r"arm_compute_version.*\n", libarm_so_strings)
        version = version_match.group(0).split(" ")[0]
        return version


#######################################################################################################################################
# The following two lists will be generated.
#
# inputs: [[test_data_0_input_0.pb, test_data_0_input_1.pb ...], [test_data_1_input_0.pb, test_data_1_input_1.pb ...] ...]
# outputs: [[test_data_0_output_0.pb, test_data_0_output_1.pb ...], [test_data_1_output_0.pb, test_data_1_output_1.pb ...] ...]
#######################################################################################################################################
def load_onnx_model_zoo_test_data(path, all_inputs_shape, fp16):
    logger.info("Parsing test data in {} ...".format(path))
    output = get_output(["find", path, "-name", "test_data*", "-type", "d"])
    test_data_set_dir = split_and_sort_output(output)
    logger.info(test_data_set_dir)

    inputs = []
    outputs = []

    shape_flag = False
    # if not empty means input shape has been parsed before.
    if len(all_inputs_shape) > 0:
        shape_flag = True

    # find test data path
    for test_data_dir in test_data_set_dir:
        pwd = os.getcwd()
        os.chdir(test_data_dir)

        # load inputs and create bindings
        output = get_output(["find", ".", "-name", "input*"])
        input_data = split_and_sort_output(output)
        logger.info(input_data)

        input_data_pb = []
        i = 0
        for data in input_data:
            tensor = onnx.TensorProto()
            with open(data, "rb") as f:
                tensor.ParseFromString(f.read())
                tensor_to_array = numpy_helper.to_array(tensor)
                if fp16 and tensor_to_array.dtype == np.dtype(np.float32):
                    tensor_to_array = tensor_to_array.astype(np.float16)
                tensor_to_array.tofile(str(i) + ".bin")
                input_data_pb.append(tensor_to_array)
                if not shape_flag:
                    all_inputs_shape.append(input_data_pb[-1].shape)
                logger.info(all_inputs_shape[-1])
        inputs.append(input_data_pb)
        logger.info("Loaded {} inputs successfully.".format(len(inputs)))

        # load outputs
        output = get_output(["find", ".", "-name", "output*"])
        output_data = split_and_sort_output(output)

        if len(output_data) > 0 and output_data[0] != "":
            logger.info(output_data)
            output_data_pb = []
            for data in output_data:
                tensor = onnx.TensorProto()
                with open(data, "rb") as f:
                    tensor.ParseFromString(f.read())

                    tensor_to_array = numpy_helper.to_array(tensor)

                    if fp16 and tensor_to_array.dtype == np.dtype(np.float32):
                        tensor_to_array = tensor_to_array.astype(np.float16)
                    output_data_pb.append(tensor_to_array)

                    logger.info(np.array(output_data_pb[-1]).shape)
            outputs.append(output_data_pb)
            logger.info("Loaded {} outputs successfully.".format(len(outputs)))

        os.chdir(pwd)
    return inputs, outputs


def generate_onnx_model_random_input(test_times, ref_input):
    inputs = []

    for i in range(test_times):

        input_data = []
        for tensor in ref_input:
            shape = tensor.shape
            dtype = tensor.dtype
            if (
                dtype == np.int8
                or dtype == np.uint8
                or dtype == np.int16
                or dtype == np.uint16
                or dtype == np.int32
                or dtype == np.uint32
                or dtype == np.int64
                or dtype == np.uint64
            ):
                new_tensor = np.random.randint(0, np.max(tensor) + 1, shape, dtype)
            else:
                new_tensor = np.random.random_sample(shape).astype(dtype)

            if debug:
                logger.info("original tensor:")
                logger.info(tensor)
                logger.info("new random tensor:")
                logger.info(new_tensor)
                logger.info("\n")

            input_data.append(new_tensor)
        inputs.append(input_data)

    return inputs


def percentage_in_allowed_threshold(e, percent_mismatch):
    percent_string = re.search(r"\(([^)]+)", str(e)).group(1)
    if "%" in percent_string:
        percentage_wrong = float(percent_string.replace("%", ""))
        return percentage_wrong < percent_mismatch
    else:
        return False  # error in output


def validate(all_ref_outputs, all_outputs, rtol, atol, percent_mismatch):
    if len(all_ref_outputs) == 0:
        logger.info("No reference output provided.")
        return True, None

    logger.info("Reference {} results.".format(len(all_ref_outputs)))
    logger.info("Predicted {} results.".format(len(all_outputs)))
    logger.info("rtol: {}, atol: {}".format(rtol, atol))

    for i in range(len(all_outputs)):
        ref_outputs = all_ref_outputs[i]
        outputs = all_outputs[i]

        for j in range(len(outputs)):
            ref_output = ref_outputs[j]
            output = outputs[j]

            # Compare the results with reference outputs
            for ref_o, o in zip(ref_output, output):
                # abs(desired-actual) < rtol * abs(desired) + atol
                try:
                    np.testing.assert_allclose(ref_o, o, rtol, atol)
                except Exception as e:
                    if percentage_in_allowed_threshold(e, percent_mismatch):
                        continue
                    logger.error(e)
                    return False, e

    logger.info("ONNX Runtime outputs are similar to reference outputs!")
    return True, None


# not use for this script
def cleanup_files():
    files = []
    p = subprocess.Popen(["find", ".", "-name", "test_data_set*", "-type", "d"], stdout=subprocess.PIPE)
    stdout, sterr = p.communicate()
    stdout = stdout.decode("ascii").strip()
    files = files + stdout.split("\n")

    p = subprocess.Popen(["find", ".", "-name", "*.onnx"], stdout=subprocess.PIPE)
    stdout, sterr = p.communicate()
    stdout = stdout.decode("ascii").strip()
    files = files + stdout.split("\n")

    p = subprocess.Popen(["find", ".", "-name", "*.gz"], stdout=subprocess.PIPE)
    stdout, sterr = p.communicate()
    stdout = stdout.decode("ascii").strip()
    files = files + stdout.split("\n")

    for f in files:
        if "custom_test_data" in f:
            logger.info(f)
            continue
        subprocess.Popen(["rm", "-rf", f], stdout=subprocess.PIPE)


def remove_files(running_mode, path):
    files = []
    out = ""
    if running_mode == "validate":
        out = get_output(["find", path, "-name", "onnxruntime_profile*"])
    if running_mode == "benchmark":
        logger.info(running_mode)
        out = get_output(["find", path, "-name", "*.engine"])


def update_fail_report(fail_results, model, ep, e_type, e):
    result = {}

    result["model"] = model
    result["ep"] = ep
    result["error type"] = e_type
    result["error message"] = re.sub("^\n", "", str(e))

    fail_results.append(result)


def update_op_metrics_map(model_to_metrics, model_name, ep_to_operator):
    if len(ep_to_operator) == 0:
        return

    if model_name not in model_to_metrics:
        model_to_metrics[model_name] = {}

    for ep, op_map in ep_to_operator.items():
        if ep not in model_to_metrics[model_name]:
            model_to_metrics[model_name][ep] = {}

        model_to_metrics[model_name][ep]["op_breakdown"] = get_op_breakdown(op_map)


###################################################################################################
#
# model: {ep1: {error_type: xxx, error_message: xxx}, ep2: {error_type: xx, error_message: xx}}
#
###################################################################################################
def update_fail_model_map(model_to_fail_ep, model_name, ep, e_type, e):

    if model_name in model_to_fail_ep and ep in model_to_fail_ep[model_name]:
        return

    if model_name not in model_to_fail_ep:
        model_to_fail_ep[model_name] = {}

    new_map = {}
    new_map["error_type"] = e_type
    new_map["error_message"] = re.sub("^\n", "", str(e))
    model_to_fail_ep[model_name][ep] = new_map

    # If TRT fails, TRT FP16 should fail as well
    if ep == trt:
        ep_ = trt_fp16
        e_ = "skip benchmarking since TRT failed already."
        new_map_1 = {}
        new_map_1["error_type"] = e_type
        new_map_1["error_message"] = e_
        model_to_fail_ep[model_name][ep_] = new_map_1


def update_fail_model_map_ori(model_to_fail_ep, fail_results, model_name, ep, e_type, e):

    if model_name in model_to_fail_ep and ep in model_to_fail_ep[model_name]:
        return

    if model_name not in model_to_fail_ep:
        model_to_fail_ep[model_name] = {}

    model_to_fail_ep[model_name][ep] = e_type
    update_fail_report(fail_results, model_name, ep, e_type, e)

    # If TRT fails, TRT FP16 should fail as well
    if ep == trt:
        ep_ = trt_fp16
        error_message_ = "skip benchmarking since TRT failed already."
        update_fail_report(fail_results, model_name, ep_, e_type, error_message_)
        model_to_fail_ep[model_name][ep_] = e_type


def skip_ep(model_name, ep, model_to_fail_ep):

    if model_name not in model_to_fail_ep:
        return False

    fail_ep_list = model_to_fail_ep[model_name]

    # if ep in fail_ep_list and fail_ep_list[ep] == "runtime error":
    if ep in fail_ep_list:
        logger.info("Skip testing " + model_name + " using " + ep + " since it has some issues.")
        return True

    return False


def read_map_from_file(map_file):
    with open(map_file) as f:
        try:
            data = json.load(f)
        except Exception as e:
            return None

    return data


def write_map_to_file(result, file_name):
    existed_result = {}
    if os.path.exists(file_name):
        existed_result = read_map_from_file(file_name)

    for model, ep_list in result.items():
        if model in existed_result:
            existed_result[model] = {**existed_result[model], **result[model]}
        else:
            existed_result[model] = result[model]

    with open(file_name, "w") as file:
        file.write(json.dumps(existed_result))  # use `json.loads` to do the reverse


def get_cuda_version():
    nvidia_strings = get_output(["nvidia-smi"])
    version = re.search(r"CUDA Version: \d\d\.\d", nvidia_strings).group(0)
    return version


def get_trt_version(workspace):
    libnvinfer = get_output(["find", workspace, "-name", "libnvinfer.so.*"])
    nvinfer = re.search(r".*libnvinfer.so.*", libnvinfer).group(0)
    trt_strings = get_output(["nm", "-D", nvinfer])
    version = re.search(r"tensorrt_version.*", trt_strings).group(0)
    return version


def get_linux_distro():
    linux_strings = get_output(["cat", "/etc/os-release"])
    stdout = linux_strings.split("\n")[:2]
    infos = []
    for row in stdout:
        row = re.sub("=", ":  ", row)
        row = re.sub('"', "", row)
        infos.append(row)
    return infos


def get_memory_info():
    mem_strings = get_output(["cat", "/proc/meminfo"])
    stdout = mem_strings.split("\n")
    infos = []
    for row in stdout:
        if "Mem" in row:
            row = re.sub(": +", ":  ", row)
            infos.append(row)
    return infos


def get_cpu_info():
    cpu_strings = get_output(["lscpu"])
    stdout = cpu_strings.split("\n")
    infos = []
    for row in stdout:
        if "mode" in row or "Arch" in row or "name" in row:
            row = re.sub(": +", ":  ", row)
            infos.append(row)
    return infos


def get_gpu_info():
    info = get_output(["lspci", "-v"])
    infos = re.findall("NVIDIA.*", info)
    return infos


def get_cudnn_version(workspace):
    cudnn_path = get_output(["whereis", "cudnn_version.h"])
    cudnn_path = re.search(": (.*)", cudnn_path).group(1)
    cudnn_outputs = get_output(["cat", cudnn_path])
    major = re.search("CUDNN_MAJOR (.*)", cudnn_outputs).group(1)
    minor = re.search("CUDNN_MINOR (.*)", cudnn_outputs).group(1)
    patch = re.search("CUDNN_PATCHLEVEL (.*)", cudnn_outputs).group(1)
    cudnn_version = major + "." + minor + "." + patch
    return cudnn_version


def get_system_info(args):
    info = {}
    info["cuda"] = get_cuda_version()
    info["trt"] = get_trt_version(args.workspace)
    info["cudnn"] = get_cudnn_version(args.workspace)
    info["linux_distro"] = get_linux_distro()
    info["cpu_info"] = get_cpu_info()
    info["gpu_info"] = get_gpu_info()
    info["memory"] = get_memory_info()
    info["ep_option_overrides"] = {
        trt_ep: args.trt_ep_options,
        cuda_ep: args.cuda_ep_options,
    }

    return info


def find_model_path(path):
    output = get_output(["find", "-L", path, "-name", "*.onnx"])
    model_path = split_and_sort_output(output)
    logger.info(model_path)

    if model_path == [""]:
        return None

    target_model_path = []
    for m in model_path:
        if "by_trt_perf" in m or m.startswith("."):
            continue
        target_model_path.append(m)

    logger.info(target_model_path)
    if len(target_model_path) > 1:
        logger.error("We expect to find only one model in " + path)
        raise

    return target_model_path[0]


def find_model_directory(path):
    output = get_output(
        [
            "find",
            "-L",
            path,
            "-maxdepth",
            "1",
            "-mindepth",
            "1",
            "-name",
            "*",
            "-type",
            "d",
        ]
    )
    model_dir = split_and_sort_output(output)
    if model_dir == [""]:
        return None

    return model_dir


def find_test_data_directory(path):
    output = get_output(["find", "-L", path, "-maxdepth", "1", "-name", "test_data*", "-type", "d"])
    test_data_dir = split_and_sort_output(output)
    logger.info(test_data_dir)

    if test_data_dir == [""]:
        return None

    return test_data_dir


def parse_models_info_from_directory(path, models):

    test_data_dir = find_test_data_directory(path)

    if test_data_dir:
        model_name = os.path.split(path)[-1]
        model_name = model_name + "_" + os.path.split(os.path.split(path)[0])[-1]  # get opset version as model_name
        model_path = find_model_path(path)

        model = {}
        model["model_name"] = model_name
        model["model_path"] = model_path
        model["working_directory"] = path
        model["test_data_path"] = path

        models[model_name] = model

        logger.info(model)
        return

    model_dir = find_model_directory(path)

    if model_dir:
        for dir in model_dir:
            parse_models_info_from_directory(os.path.join(path, dir), models)


def parse_models_info_from_file(root_dir, path, models):

    # default working directory
    root_working_directory = root_dir + "perf/"

    with open(path) as f:
        data = json.load(f)

        for row in data:

            if "root_working_directory" in row:
                root_working_directory = row["root_working_directory"]
                continue

            if "model_name" in row:
                models[row["model_name"]] = {}
            else:
                logger.error("Model name must be provided in models_info.json")
                raise

            model = models[row["model_name"]]

            if "working_directory" in row:
                if os.path.isabs(row["working_directory"]):
                    model["working_directory"] = row["working_directory"]
                else:
                    model["working_directory"] = os.path.join(root_working_directory, row["working_directory"])
            else:
                logger.error("Model path must be provided in models_info.json")
                raise

            if "model_path" in row:
                model["model_path"] = row["model_path"]
            else:
                logger.error("Model path must be provided in models_info.json")
                raise

            if "test_data_path" in row:
                model["test_data_path"] = row["test_data_path"]
            else:
                logger.error("Test data path must be provided in models_info.json")
                raise

            if "model_path_fp16" in row:
                model["model_path_fp16"] = row["model_path_fp16"]

            if "test_data_path_fp16" in row:
                model["test_data_path_fp16"] = row["test_data_path_fp16"]


def convert_model_from_float_to_float16(model_path):
    from float16 import convert_float_to_float16
    from onnxmltools.utils import load_model, save_model

    new_model_path = os.path.join(os.getcwd(), "new_fp16_model_by_trt_perf.onnx")
    if not os.path.exists(new_model_path):
        onnx_model = load_model(model_path)
        new_onnx_model = convert_float_to_float16(onnx_model)
        save_model(new_onnx_model, "new_fp16_model_by_trt_perf.onnx")

    return new_model_path


def get_test_data(fp16, test_data_dir, all_inputs_shape):
    inputs = []
    ref_outputs = []
    inputs, ref_outputs = load_onnx_model_zoo_test_data(test_data_dir, all_inputs_shape, fp16)
    return inputs, ref_outputs


def run_symbolic_shape_inference(model_path, new_model_path):
    import onnxruntime.tools.symbolic_shape_infer as symbolic_shape_infer

    logger.info("run symbolic shape inference")
    try:
        out = symbolic_shape_infer.SymbolicShapeInference.infer_shapes(onnx.load(model_path), auto_merge=True)
        onnx.save(out, new_model_path)
        return True, None
    except Exception as e:
        logger.error(e)
        return False, "Symbolic shape inference error"


def get_provider_options(providers, trt_ep_options, cuda_ep_options):
    provider_options = []

    for ep in providers:
        if ep == trt_ep:
            provider_options.append(trt_ep_options)
        elif ep == cuda_ep:
            provider_options.append(cuda_ep_options)
        else:
            provider_options.append({})

    return provider_options


def time_and_create_session(model_path, providers, provider_options, session_options):
    start = datetime.now()
    session = onnxruntime.InferenceSession(
        model_path,
        providers=providers,
        provider_options=provider_options,
        sess_options=session_options,
    )
    end = datetime.now()
    creation_time = (end - start).total_seconds()
    return session, creation_time


def create_session(model_path, providers, provider_options, session_options):
    logger.info(model_path)

    try:
        return time_and_create_session(model_path, providers, provider_options, session_options)
    except Exception as e:
        # shape inference required on model
        if "shape inference" in str(e):
            logger.info("Using model from symbolic_shape_infer.py")
            new_model_path = model_path[:].replace(".onnx", "_new_by_trt_perf.onnx")
            if not os.path.exists(new_model_path):
                status = run_symbolic_shape_inference(model_path, new_model_path)
                if not status[0]:  # symbolic shape inference error
                    e = status[1]
                    raise Exception(e)
            return time_and_create_session(new_model_path, providers, provider_options, session_options)
        else:
            raise Exception(e)


def run_onnxruntime(args, models):

    success_results = []
    model_to_latency = {}  # model -> cuda and tensorrt latency
    model_to_metrics = {}  # model -> metrics from profiling file
    model_to_fail_ep = {}  # model -> failing ep
    model_to_session = {}  # models -> session creation time

    if args.running_mode == "benchmark":
        model_to_session = read_map_from_file(SESSION_FILE)

    ep_list = []
    if args.ep:
        ep_list.append(args.ep)
    else:
        if args.fp16:
            ep_list = [cpu, cuda, trt, cuda_fp16, trt_fp16]
        else:
            ep_list = [cpu, cuda, trt]

    validation_exemption = [trt_fp16]

    if os.path.exists(FAIL_MODEL_FILE):
        model_to_fail_ep = read_map_from_file(FAIL_MODEL_FILE)

    #######################
    # iterate model
    #######################
    for name, model_info in models.items():
        latency_result = {}
        path = model_info["working_directory"]

        pwd = os.getcwd()
        if not os.path.exists(path):
            os.mkdir(path)
        os.chdir(path)
        path = os.getcwd()

        inputs = []
        ref_outputs = []
        all_inputs_shape = []  # use for standalone trt
        ep_to_operator = {}  # ep -> { operator -> count }
        profile_already_parsed = set()

        #######################
        # iterate ep
        #######################
        for ep in ep_list:
            if skip_ep(name, ep, model_to_fail_ep):
                continue

            if not is_standalone(ep):
                ep_ = ep_to_provider_list[ep][0]
                if ep_ not in onnxruntime.get_available_providers():
                    logger.error("No {} support".format(ep_))
                    continue

            model_path = model_info["model_path"]
            test_data_dir = model_info["test_data_path"]

            logger.info("[Initialize]  model = {}, ep = {} ...".format(name, ep))

            # Set environment variables for ort-trt benchmarking
            trt_ep_options = copy.deepcopy(args.trt_ep_options)
            if "ORT-TRT" in ep:
                trt_ep_options["trt_fp16_enable"] = "True" if "Fp16" in ep else "False"

            fp16 = False

            # use float16.py for cuda fp16 only
            if cuda_fp16 == ep:

                # handle model
                if "model_path_fp16" in model_info:
                    model_path = model_info["model_path_fp16"]

                else:
                    try:
                        model_path = convert_model_from_float_to_float16(model_path)
                        fp16 = True
                    except Exception as e:
                        logger.error(e)
                        update_fail_model_map(model_to_fail_ep, name, ep, "script error", e)
                        continue

                # handle test data
                if "test_data_path_fp16" in model_info:
                    test_data_dir = model_info["test_data_path_fp16"]
                    fp16 = False

            if standalone_trt_fp16 == ep:
                fp16 = True

            inputs, ref_outputs = get_test_data(fp16, test_data_dir, all_inputs_shape)
            # generate random input data
            if args.input_data == "random":
                inputs = generate_onnx_model_random_input(args.test_times, inputs[0])

            #######################################
            # benchmark or validation
            #######################################
            if args.running_mode == "benchmark":
                logger.info("\n----------------------------- benchmark -------------------------------------")

                # memory tracking variables
                p = None
                mem_usage = None
                result = None

                # get standalone TensorRT perf
                if is_standalone(ep) and args.trtexec:
                    try:
                        result = run_trt_standalone(
                            args.trtexec,
                            name,
                            model_path,
                            all_inputs_shape,
                            fp16,
                            args.track_memory,
                        )
                    except Exception as e:
                        logger.error(e)
                        update_fail_model_map(model_to_fail_ep, name, ep, "runtime error", e)
                        continue

                # inference with onnxruntime ep
                else:
                    # resolve providers to create session
                    providers = ep_to_provider_list[ep]
                    provider_options = get_provider_options(providers, trt_ep_options, args.cuda_ep_options)
                    options = onnxruntime.SessionOptions()

                    enablement = args.graph_enablement
                    if enablement == enable_all:
                        options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
                    elif enablement == extended:
                        options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
                    elif enablement == basic:
                        options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_BASIC
                    else:  # disable
                        options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_DISABLE_ALL

                    # create onnxruntime inference session
                    try:
                        sess, second_creation_time = create_session(model_path, providers, provider_options, options)

                    except Exception as e:
                        logger.error(e)
                        update_fail_model_map(model_to_fail_ep, name, ep, "runtime error", e)
                        continue

                    if second_creation_time:
                        model_to_session[name] = copy.deepcopy({ep + second: second_creation_time})

                    logger.info("start to inference {} with {} ...".format(name, ep))
                    logger.info(sess.get_providers())
                    logger.info(sess.get_provider_options())

                    if sess:
                        logger.info("Model inputs nodes:")
                        for input_meta in sess.get_inputs():
                            logger.info(input_meta)
                        logger.info("Model outputs nodes:")
                        for output_meta in sess.get_outputs():
                            logger.info(output_meta)

                    batch_size = 1
                    result_template = {
                        "engine": "onnxruntime",
                        "version": onnxruntime.__version__,
                        "device": ep,
                        "fp16": fp16,
                        "io_binding": args.io_binding,
                        "graph_optimizations": args.graph_enablement,
                        "enable_cache": args.trt_ep_options.get("trt_engine_cache_enable", "False"),
                        "model_name": name,
                        "inputs": len(sess.get_inputs()),
                        "batch_size": batch_size,
                        "sequence_length": 1,
                        "datetime": str(datetime.now()),
                    }

                    # run cpu fewer times
                    repeat_times = 100 if ep == cpu else args.test_times
                    track_memory = False if ep == cpu else args.track_memory

                    # inference with ort
                    try:
                        result, mem_usage = inference_ort(
                            args,
                            name,
                            sess,
                            ep,
                            inputs,
                            result_template,
                            repeat_times,
                            batch_size,
                            track_memory,
                        )
                    except Exception as e:
                        logger.error(e)
                        update_fail_model_map(model_to_fail_ep, name, ep, "runtime error", e)
                        continue

                if result:

                    latency_result[ep] = {}
                    latency_result[ep]["average_latency_ms"] = result["average_latency_ms"]
                    latency_result[ep]["latency_90_percentile"] = result["latency_90_percentile"]
                    if "memory" in result:
                        mem_usage = result["memory"]
                    if mem_usage:
                        latency_result[ep]["memory"] = mem_usage
                    if not args.trtexec:  # skip standalone
                        success_results.append(result)

                    model_to_latency[name] = copy.deepcopy(latency_result)

                    if ep == trt_fp16:  # delete engine
                        remove_files(args.running_mode, model_info["working_directory"])

                logger.info("---------------------------- benchmark [end] ----------------------------------\n")

            elif args.running_mode == "validate":
                logger.info("\n----------------------------- validate -------------------------------------")

                # enable profiling to generate profiling file for analysis
                options = onnxruntime.SessionOptions()
                options.enable_profiling = True
                options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
                time.sleep(1)  # avoid to generate same profile file name

                providers = ep_to_provider_list[ep]
                provider_options = get_provider_options(providers, trt_ep_options, args.cuda_ep_options)

                # create onnxruntime inference session
                try:
                    sess, creation_time = create_session(model_path, providers, provider_options, options)

                except Exception as e:
                    logger.error(e)
                    update_fail_model_map(model_to_fail_ep, name, ep, "runtime error", e)
                    continue

                if creation_time:
                    model_to_session[name] = copy.deepcopy({ep: creation_time})

                sess.disable_fallback()

                logger.info("start to inference {} with {} ...".format(name, ep))
                logger.info(sess.get_providers())
                logger.info(sess.get_provider_options())

                if sess:
                    logger.info("Model inputs nodes:")
                    for input_meta in sess.get_inputs():
                        logger.info(input_meta)
                    logger.info("Model outputs nodes:")
                    for output_meta in sess.get_outputs():
                        logger.info(output_meta)

                # run inference and validate the result
                #
                # currently skip TensorRT float16 validation intentionally
                if ep not in validation_exemption:
                    try:
                        ort_outputs = inference_ort_and_get_prediction(name, sess, inputs)

                        status = validate(
                            ref_outputs,
                            ort_outputs,
                            args.rtol,
                            args.atol,
                            args.percent_mismatch,
                        )
                        if not status[0]:
                            remove_files(args.running_mode, model_info["working_directory"])
                            update_fail_model_map(
                                model_to_fail_ep,
                                name,
                                ep,
                                "result accuracy issue",
                                status[1],
                            )
                            continue
                    except Exception as e:
                        logger.error(e)
                        update_fail_model_map(model_to_fail_ep, name, ep, "runtime error", e)
                        continue

                    # Run inference again. the reason is that some ep like tensorrt
                    # it takes much longer time to generate graph on first run and
                    # we need to skip the perf result of that expensive run.
                    inference_ort_and_get_prediction(name, sess, inputs)
                else:
                    inference_ort_and_get_prediction(name, sess, inputs)
                    inference_ort_and_get_prediction(name, sess, inputs)

                sess.end_profiling()

                # get metrics from profiling file
                metrics = get_profile_metrics(path, profile_already_parsed, logger)
                if metrics:
                    logger.info(ep)
                    ep_to_operator[ep] = metrics

                remove_files(args.running_mode, model_info["working_directory"])
                logger.info("---------------------------- validate [end] ----------------------------------\n")

        ####################
        # end of iterate ep
        ####################

        # get percentage of execution time and operators in TRT
        update_op_metrics_map(model_to_metrics, name, ep_to_operator)

        # cleanup_files()
        os.chdir(pwd)

        # end of model

    return (
        success_results,
        model_to_latency,
        model_to_fail_ep,
        model_to_metrics,
        model_to_session,
    )


def calculate_gain(value, ep1, ep2):
    ep1_latency = float(value[ep1]["average_latency_ms"])
    ep2_latency = float(value[ep2]["average_latency_ms"])
    gain = (ep2_latency - ep1_latency) * 100 / ep2_latency
    return gain


def add_improvement_information(model_to_latency):
    for key, value in model_to_latency.items():
        if "ORT-TRT" in value and "ORT-CUDA" in value:
            gain = calculate_gain(value, trt, cuda)
            value[trt_cuda_gain] = "{:.2f} %".format(gain)
            if trt_fp16 in value and cuda_fp16 in value:
                gain = calculate_gain(value, trt_fp16, cuda_fp16)
                value[trt_cuda_fp16_gain] = "{:.2f} %".format(gain)
        if "ORT-TRT" in value and is_standalone(value):
            gain = calculate_gain(value, trt, standalone_trt)
            value[trt_native_gain] = "{:.2f} %".format(gain)
            if trt_fp16 in value and standalone_trt_fp16 in value:
                gain = calculate_gain(value, trt_fp16, standalone_trt_fp16)
                value[trt_native_fp16_gain] = "{:.2f} %".format(gain)


def output_details(results, csv_filename):
    need_write_header = True
    if os.path.exists(csv_filename):
        need_write_header = False

    with open(csv_filename, mode="a", newline="") as csv_file:
        column_names = [
            "engine",
            "version",
            "device",
            "fp16",
            "io_binding",
            "graph_optimizations",
            "enable_cache",
            "model_name",
            "inputs",
            "batch_size",
            "sequence_length",
            "datetime",
            "test_times",
            "QPS",
            "average_latency_ms",
            "latency_variance",
            "latency_90_percentile",
            "latency_95_percentile",
            "latency_99_percentile",
        ]

        csv_writer = csv.DictWriter(csv_file, fieldnames=column_names)
        if need_write_header:
            csv_writer.writeheader()
        for result in results:
            csv_writer.writerow(result)


def output_fail(model_to_fail_ep, csv_filename):

    with open(csv_filename, mode="w", newline="") as csv_file:
        column_names = ["model", "ep", "error type", "error message"]

        csv_writer = csv.DictWriter(csv_file, fieldnames=column_names)
        csv_writer.writeheader()

        for model, model_info in model_to_fail_ep.items():
            for ep, ep_info in model_info.items():
                result = {}
                result["model"] = model
                result["ep"] = ep
                result["error type"] = ep_info["error_type"]
                result["error message"] = ep_info["error_message"]
                csv_writer.writerow(result)


def read_success_from_file(success_file):
    success_results = []
    with open(success_file) as success:
        csv_reader = csv.DictReader(success)
        for row in csv_reader:
            success_results.append(row)

    success_json = json.loads(json.dumps(success_results, indent=4))
    return success_json


def add_status_dict(status_dict, model_name, ep, status):
    if model_name not in status_dict:
        status_dict[model_name] = {}
    status_dict[model_name][ep] = status


def build_status(status_dict, results, is_fail):

    if is_fail:
        for model, model_info in results.items():
            for ep, ep_info in model_info.items():
                model_name = model
                ep = ep
                status = "Fail"
                add_status_dict(status_dict, model_name, ep, status)
    else:
        for model, value in results.items():
            for ep, ep_info in value.items():
                model_name = model
                ep = ep
                status = "Pass"
                add_status_dict(status_dict, model_name, ep, status)

    return status_dict


def output_status(results, csv_filename):

    need_write_header = True
    if os.path.exists(csv_filename):
        need_write_header = False

    with open(csv_filename, mode="a", newline="") as csv_file:
        column_names = table_headers

        csv_writer = csv.writer(csv_file)

        if need_write_header:
            csv_writer.writerow(column_names)

        cpu_status = ""
        cuda_fp32_status = ""
        trt_fp32_status = ""
        standalone_fp32_status = ""
        cuda_fp16_status = ""
        trt_fp16_status = ""
        standalone_fp16_status = ""

        for model_name, ep_dict in results.items():
            for ep, status in ep_dict.items():
                if ep == cpu:
                    cpu_status = status
                elif ep == cuda:
                    cuda_fp32_status = status
                elif ep == trt:
                    trt_fp32_status = status
                elif ep == standalone_trt:
                    standalone_fp32_status = status
                elif ep == cuda_fp16:
                    cuda_fp16_status = status
                elif ep == trt_fp16:
                    trt_fp16_status = status
                elif ep == standalone_trt_fp16:
                    standalone_fp16_status = status
                else:
                    continue

            row = [
                model_name,
                cpu_status,
                cuda_fp32_status,
                trt_fp32_status,
                standalone_fp32_status,
                cuda_fp16_status,
                trt_fp16_status,
                standalone_fp16_status,
            ]
            csv_writer.writerow(row)


def output_specs(info, csv_filename):
    cpu_version = info["cpu_info"][2]
    gpu_version = info["gpu_info"][0]
    tensorrt_version = info["trt"] + " , *All ORT-TRT and TRT are run in Mixed Precision mode (Fp16 and Fp32)."
    cuda_version = info["cuda"]
    cudnn_version = info["cudnn"]
    ep_option_overrides = json.dumps(info["ep_option_overrides"])

    table = pd.DataFrame(
        {
            ".": [1, 2, 3, 4, 5, 6],
            "Spec": ["CPU", "GPU", "TensorRT", "CUDA", "CuDNN", "EPOptionOverrides"],
            "Version": [
                cpu_version,
                gpu_version,
                tensorrt_version,
                cuda_version,
                cudnn_version,
                ep_option_overrides,
            ],
        }
    )
    table.to_csv(csv_filename, index=False)


def output_session_creation(results, csv_filename):
    need_write_header = True
    if os.path.exists(csv_filename):
        need_write_header = False

    with open(csv_filename, mode="a", newline="") as csv_file:
        session_1 = [p + session_ending for p in ort_provider_list]
        session_2 = [p + second_session_ending for p in ort_provider_list]
        column_names = [model_title] + session_1 + session_2
        csv_writer = csv.writer(csv_file)

        csv_writer = csv.writer(csv_file)

        if need_write_header:
            csv_writer.writerow(column_names)

        cpu_time = ""
        cuda_fp32_time = ""
        trt_fp32_time = ""
        cuda_fp16_time = ""
        trt_fp16_time = ""
        cpu_time_2 = ""
        cuda_fp32_time_2 = ""
        trt_fp32_time_2 = ""
        cuda_fp16_time_2 = ""
        trt_fp16_time_2 = ""

        for model_name, ep_dict in results.items():
            for ep, time in ep_dict.items():
                if ep == cpu:
                    cpu_time = time
                elif ep == cuda:
                    cuda_fp32_time = time
                elif ep == trt:
                    trt_fp32_time = time
                elif ep == cuda_fp16:
                    cuda_fp16_time = time
                elif ep == trt_fp16:
                    trt_fp16_time = time
                if ep == cpu + second:
                    cpu_time_2 = time
                elif ep == cuda + second:
                    cuda_fp32_time_2 = time
                elif ep == trt + second:
                    trt_fp32_time_2 = time
                elif ep == cuda_fp16 + second:
                    cuda_fp16_time_2 = time
                elif ep == trt_fp16 + second:
                    trt_fp16_time_2 = time
                else:
                    continue

            row = [
                model_name,
                cpu_time,
                cuda_fp32_time,
                trt_fp32_time,
                cuda_fp16_time,
                trt_fp16_time,
                cpu_time_2,
                cuda_fp32_time_2,
                trt_fp32_time_2,
                cuda_fp16_time_2,
                trt_fp16_time_2,
            ]
            csv_writer.writerow(row)


def output_latency(results, csv_filename):
    need_write_header = True
    if os.path.exists(csv_filename):
        need_write_header = False

    with open(csv_filename, mode="a", newline="") as csv_file:
        column_names = [model_title]
        for provider in provider_list:
            column_names.append(provider + avg_ending)
            column_names.append(provider + percentile_ending)
            if cpu not in provider:
                column_names.append(provider + memory_ending)

        csv_writer = csv.writer(csv_file)

        if need_write_header:
            csv_writer.writerow(column_names)

        for key, value in results.items():
            cpu_average = ""
            if cpu in value and "average_latency_ms" in value[cpu]:
                cpu_average = value[cpu]["average_latency_ms"]

            cpu_90_percentile = ""
            if cpu in value and "latency_90_percentile" in value[cpu]:
                cpu_90_percentile = value[cpu]["latency_90_percentile"]

            cuda_average = ""
            if cuda in value and "average_latency_ms" in value[cuda]:
                cuda_average = value[cuda]["average_latency_ms"]

            cuda_90_percentile = ""
            if cuda in value and "latency_90_percentile" in value[cuda]:
                cuda_90_percentile = value[cuda]["latency_90_percentile"]

            cuda_memory = ""
            if cuda in value and "memory" in value[cuda]:
                cuda_memory = value[cuda]["memory"]

            trt_average = ""
            if trt in value and "average_latency_ms" in value[trt]:
                trt_average = value[trt]["average_latency_ms"]

            trt_90_percentile = ""
            if trt in value and "latency_90_percentile" in value[trt]:
                trt_90_percentile = value[trt]["latency_90_percentile"]

            trt_memory = ""
            if trt in value and "memory" in value[trt]:
                trt_memory = value[trt]["memory"]

            standalone_trt_average = ""
            if standalone_trt in value and "average_latency_ms" in value[standalone_trt]:
                standalone_trt_average = value[standalone_trt]["average_latency_ms"]

            standalone_trt_90_percentile = ""
            if standalone_trt in value and "latency_90_percentile" in value[standalone_trt]:
                standalone_trt_90_percentile = value[standalone_trt]["latency_90_percentile"]

            standalone_trt_memory = ""
            if standalone_trt in value and "memory" in value[standalone_trt]:
                standalone_trt_memory = value[standalone_trt]["memory"]

            cuda_fp16_average = ""
            if cuda_fp16 in value and "average_latency_ms" in value[cuda_fp16]:
                cuda_fp16_average = value[cuda_fp16]["average_latency_ms"]

            cuda_fp16_memory = ""
            if cuda_fp16 in value and "memory" in value[cuda_fp16]:
                cuda_fp16_memory = value[cuda_fp16]["memory"]

            cuda_fp16_90_percentile = ""
            if cuda_fp16 in value and "latency_90_percentile" in value[cuda_fp16]:
                cuda_fp16_90_percentile = value[cuda_fp16]["latency_90_percentile"]

            trt_fp16_average = ""
            if trt_fp16 in value and "average_latency_ms" in value[trt_fp16]:
                trt_fp16_average = value[trt_fp16]["average_latency_ms"]

            trt_fp16_90_percentile = ""
            if trt_fp16 in value and "latency_90_percentile" in value[trt_fp16]:
                trt_fp16_90_percentile = value[trt_fp16]["latency_90_percentile"]

            trt_fp16_memory = ""
            if trt_fp16 in value and "memory" in value[trt_fp16]:
                trt_fp16_memory = value[trt_fp16]["memory"]

            standalone_trt_fp16_average = ""
            if standalone_trt_fp16 in value and "average_latency_ms" in value[standalone_trt_fp16]:
                standalone_trt_fp16_average = value[standalone_trt_fp16]["average_latency_ms"]

            standalone_trt_fp16_90_percentile = ""
            if standalone_trt_fp16 in value and "latency_90_percentile" in value[standalone_trt_fp16]:
                standalone_trt_fp16_90_percentile = value[standalone_trt_fp16]["latency_90_percentile"]

            standalone_trt_fp16_memory = ""
            if standalone_trt_fp16 in value and "memory" in value[standalone_trt_fp16]:
                standalone_trt_fp16_memory = value[standalone_trt_fp16]["memory"]

            row = [
                key,
                cpu_average,
                cpu_90_percentile,
                cuda_average,
                cuda_90_percentile,
                cuda_memory,
                trt_average,
                trt_90_percentile,
                trt_memory,
                standalone_trt_average,
                standalone_trt_90_percentile,
                standalone_trt_memory,
                cuda_fp16_average,
                cuda_fp16_90_percentile,
                cuda_fp16_memory,
                trt_fp16_average,
                trt_fp16_90_percentile,
                trt_fp16_memory,
                standalone_trt_fp16_average,
                standalone_trt_fp16_90_percentile,
                standalone_trt_fp16_memory,
            ]
            csv_writer.writerow(row)

    logger.info(f"CUDA/TRT latency comparison are saved to csv file: {csv_filename}")


def get_model_cuda_op_info(model, ep_info, fp16):
    cuda_key = cuda_fp16 if fp16 else cuda
    op_breakdown = ep_info[cuda_key]["op_breakdown"]
    model_cuda_op_info = {
        "model_name": model,
        "ep": cuda_key,
        "num_cpu_ops": op_breakdown[cpu_ep]["num_ops"],
        "cpu_exec_time": op_breakdown[cpu_ep]["exec_time"],
        "cpu_ops": op_breakdown[cpu_ep]["ops"],
        "num_cuda_ops": op_breakdown[cuda_ep]["num_ops"],
        "cuda_exec_time": op_breakdown[cuda_ep]["exec_time"],
        "cuda_ops": op_breakdown[cuda_ep]["ops"],
        "num_trt_ops": op_breakdown[trt_ep]["num_ops"],  # Should be 0
        "trt_exec_time": op_breakdown[trt_ep]["exec_time"],  # Should be 0
        "trt_ops": op_breakdown[trt_ep]["ops"],  # Should be empty
    }

    return model_cuda_op_info


def get_model_trt_op_info(model, ep_info, model_cuda_op_info, fp16):
    trt_key = trt_fp16 if fp16 else trt
    op_breakdown = ep_info[trt_key]["op_breakdown"]

    # Because we can't directly parse the number of trt ops from profile data, we need to
    # calculate the number of trt ops as:
    # trt_num_ops = (total num ops in ORT-CUDAFpxx) - (cuda and cpu ops in ORT-TRTFpxx)
    num_cpu_ops = op_breakdown[cpu_ep]["num_ops"]
    num_cuda_ops = op_breakdown[cuda_ep]["num_ops"]
    num_cuda_cpu_ops = num_cpu_ops + num_cuda_ops
    num_trt_ops = (model_cuda_op_info["num_cpu_ops"] + model_cuda_op_info["num_cuda_ops"]) - num_cuda_cpu_ops

    model_trt_op_info = {
        "model_name": model,
        "ep": trt_key,
        "num_cpu_ops": num_cpu_ops,
        "cpu_exec_time": op_breakdown[cpu_ep]["exec_time"],
        "cpu_ops": op_breakdown[cpu_ep]["ops"],
        "num_cuda_ops": num_cuda_ops,
        "cuda_exec_time": op_breakdown[cuda_ep]["exec_time"],
        "cuda_ops": op_breakdown[cuda_ep]["ops"],
        "num_trt_ops": num_trt_ops,
        "trt_exec_time": op_breakdown[trt_ep]["exec_time"],
        "trt_ops": op_breakdown[trt_ep]["ops"],
    }

    return model_trt_op_info


def output_metrics(model_to_metrics, csv_filename):
    with open(csv_filename, mode="w", newline="") as csv_file:
        column_names = [c[1] for c in op_metrics_columns]
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(column_names)

        results = []
        for model, ep_info in model_to_metrics.items():
            if cuda in ep_info:
                model_cuda_op_info = get_model_cuda_op_info(model, ep_info, False)
                results.append(model_cuda_op_info)

                if trt in ep_info:
                    model_trt_op_info = get_model_trt_op_info(model, ep_info, model_cuda_op_info, False)
                    results.append(model_trt_op_info)

            if cuda_fp16 in ep_info:
                model_cuda_op_info = get_model_cuda_op_info(model, ep_info, True)
                results.append(model_cuda_op_info)

                if trt_fp16 in ep_info:
                    model_trt_op_info = get_model_trt_op_info(model, ep_info, model_cuda_op_info, True)
                    results.append(model_trt_op_info)

        for value in results:
            row = [value[c[0]] for c in op_metrics_columns]
            csv_writer.writerow(row)

    logger.info(f"Tensorrt ratio metrics are saved to csv file: {csv_filename}")


def output_system_info(result, csv_filename):
    with open(csv_filename, mode="a", newline="") as csv_file:
        column_names = ["cpu_info", "cuda", "gpu_info", "linux_distro", "memory", "trt"]

        csv_writer = csv.DictWriter(csv_file, fieldnames=column_names)
        csv_writer.writeheader()
        csv_writer.writerow(result)

    logger.info(f"System information are saved to csv file: {csv_filename}")


def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif v.lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected.")


class ParseDictArgAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string):
        dict_arg = {}

        for kv in values.split(","):
            try:
                k, v = kv.split("=")
            except ValueError:
                parser.error("argument {opt_str}: Expected '=' between key and value".format(opt_str=option_string))

            if k in dict_arg:
                parser.error(
                    "argument {opt_str}: Specified duplicate key '{dup_key}'".format(opt_str=option_string, dup_key=k)
                )

            dict_arg[k] = v

        setattr(namespace, self.dest, dict_arg)


def parse_arguments():
    # Used by argparse to display usage information for custom inputs.
    dict_arg_metavar = "Opt1=Val1,Opt2=Val2..."

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-c",
        "--comparison",
        required=False,
        default="cuda_trt",
        choices=["cuda_trt", "acl"],
        help="EPs to compare: CPU vs. CUDA vs. TRT or CPU vs. ACL",
    )

    parser.add_argument(
        "-m",
        "--model_source",
        required=False,
        default="model_list.json",
        help="Model source: (1) model list file (2) model directory.",
    )

    parser.add_argument(
        "-r",
        "--running_mode",
        required=False,
        default="benchmark",
        choices=["validate", "benchmark"],
        help="Testing mode.",
    )

    parser.add_argument(
        "-i",
        "--input_data",
        required=False,
        default="fix",
        choices=["fix", "random"],
        help="Type of input data.",
    )

    parser.add_argument(
        "-o",
        "--perf_result_path",
        required=False,
        default="result",
        help="Directory for perf result.",
    )

    parser.add_argument(
        "-w",
        "--workspace",
        required=False,
        default="/",
        help="Workspace to find tensorrt and perf script (with models if parsing with model file)",
    )

    parser.add_argument(
        "-e",
        "--ep_list",
        nargs="+",
        required=False,
        default=None,
        help="Specify ORT Execution Providers list.",
    )

    parser.add_argument(
        "--trt_ep_options",
        required=False,
        default={
            "trt_engine_cache_enable": "True",
            "trt_max_workspace_size": "4294967296",
        },
        action=ParseDictArgAction,
        metavar=dict_arg_metavar,
        help="Specify options for the ORT TensorRT Execution Provider",
    )

    parser.add_argument(
        "--cuda_ep_options",
        required=False,
        default={},
        action=ParseDictArgAction,
        metavar=dict_arg_metavar,
        help="Specify options for the ORT CUDA Execution Provider",
    )

    parser.add_argument(
        "-z",
        "--track_memory",
        required=False,
        default=True,
        help="Track CUDA and TRT Memory Usage",
    )

    parser.add_argument("-b", "--io_binding", required=False, default=False, help="Bind Inputs")

    parser.add_argument(
        "-g",
        "--graph_enablement",
        required=False,
        default=enable_all,
        choices=[disable, basic, extended, enable_all],
        help="Choose graph optimization enablement.",
    )

    parser.add_argument("--ep", required=False, default=None, help="Specify ORT Execution Provider.")

    parser.add_argument(
        "--fp16",
        required=False,
        default=True,
        action="store_true",
        help="Inlcude Float16 into benchmarking.",
    )

    parser.add_argument("--trtexec", required=False, default=None, help="trtexec executable path.")

    # Validation options
    parser.add_argument(
        "--percent_mismatch",
        required=False,
        default=20.0,
        help="Allowed percentage of mismatched elements in validation.",
    )
    parser.add_argument(
        "--rtol",
        required=False,
        default=0,
        help="Relative tolerance for validating outputs.",
    )
    parser.add_argument(
        "--atol",
        required=False,
        default=20,
        help="Absolute tolerance for validating outputs.",
    )

    parser.add_argument(
        "-t",
        "--test_times",
        required=False,
        default=1,
        type=int,
        help="Number of repeat times to get average inference latency.",
    )

    parser.add_argument("--write_test_result", type=str2bool, required=False, default=True, help="")
    parser.add_argument("--benchmark_fail_csv", required=False, default=None, help="")
    parser.add_argument("--benchmark_success_csv", required=False, default=None, help="")
    parser.add_argument("--benchmark_latency_csv", required=False, default=None, help="")
    parser.add_argument("--benchmark_metrics_csv", required=False, default=None, help="")
    parser.add_argument("--system_info_csv", required=False, default=None, help="")

    args = parser.parse_args()

    return args


def setup_logger(verbose):
    if verbose:
        coloredlogs.install(
            level="DEBUG",
            fmt="[%(filename)s:%(lineno)s - %(funcName)20s()] %(message)s",
        )
    else:
        coloredlogs.install(fmt="%(message)s")
        logging.getLogger("transformers").setLevel(logging.WARNING)


def parse_models_helper(args, models):
    model_source = os.path.join(args.workspace, args.model_source)
    if ".json" in model_source:
        logger.info("Parsing model information from file ...")
        parse_models_info_from_file(args.workspace, model_source, models)
    else:
        logger.info("Parsing model information from directory ...")
        parse_models_info_from_directory(model_source, models)


def main():
    args = parse_arguments()
    setup_logger(False)
    pp = pprint.PrettyPrinter(indent=4)

    logger.info("\n\nStart perf run ...\n")

    models = {}
    parse_models_helper(args, models)

    perf_start_time = datetime.now()
    (
        success_results,
        model_to_latency,
        model_to_fail_ep,
        model_to_metrics,
        model_to_session,
    ) = run_onnxruntime(args, models)
    perf_end_time = datetime.now()

    logger.info("Done running the perf.")
    logger.info("\nTotal time for benchmarking all models: {}".format(perf_end_time - perf_start_time))
    logger.info(list(models.keys()))

    logger.info("\nTotal models: {}".format(len(models)))

    fail_model_cnt = 0
    for key, value in models.items():
        if key in model_to_fail_ep:
            fail_model_cnt += 1
    logger.info("Fail models: {}".format(fail_model_cnt))
    logger.info("Success models: {}".format(len(models) - fail_model_cnt))

    path = os.path.join(os.getcwd(), args.perf_result_path)
    if not os.path.exists(path):
        from pathlib import Path

        Path(path).mkdir(parents=True, exist_ok=True)

    time_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if len(model_to_fail_ep) > 0:
        logger.info("\n============================================")
        logger.info("========== Failing Models/EPs ==============")
        logger.info("============================================")
        logger.info(model_to_fail_ep)
        write_map_to_file(model_to_fail_ep, FAIL_MODEL_FILE)

        if args.write_test_result:
            csv_filename = args.benchmark_fail_csv if args.benchmark_fail_csv else f"benchmark_fail_{time_stamp}.csv"
            csv_filename = os.path.join(path, csv_filename)
            output_fail(model_to_fail_ep, csv_filename)

    if len(model_to_latency) > 0:
        logger.info("\n==========================================")
        logger.info("=========== Models/EPs latency ===========")
        logger.info("==========================================")
        add_improvement_information(model_to_latency)
        pretty_print(pp, model_to_latency)
        write_map_to_file(model_to_latency, LATENCY_FILE)
        if args.write_test_result:
            csv_filename = (
                args.benchmark_latency_csv if args.benchmark_latency_csv else f"benchmark_latency_{time_stamp}.csv"
            )
            csv_filename = os.path.join(path, csv_filename)
            output_latency(model_to_latency, csv_filename)

    if success_results:
        csv_filename = (
            args.benchmark_success_csv if args.benchmark_success_csv else f"benchmark_success_{time_stamp}.csv"
        )
        csv_filename = os.path.join(path, csv_filename)
        output_details(success_results, csv_filename)

    if len(model_to_metrics) > 0:
        logger.info("\n=========================================")
        logger.info("========== Models/EPs metrics  ==========")
        logger.info("=========================================")
        pretty_print(pp, model_to_metrics)
        write_map_to_file(model_to_metrics, METRICS_FILE)

        if args.write_test_result:
            csv_filename = (
                args.benchmark_metrics_csv if args.benchmark_metrics_csv else f"benchmark_metrics_{time_stamp}.csv"
            )
            csv_filename = os.path.join(path, csv_filename)
            output_metrics(model_to_metrics, csv_filename)

    if len(model_to_session) > 0:
        write_map_to_file(model_to_session, SESSION_FILE)


if __name__ == "__main__":
    main()
