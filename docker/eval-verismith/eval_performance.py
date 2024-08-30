# Copyright 2024 Flavien Solt, ETH Zurich.
# Licensed under the General Public License, Version 3.0, see LICENSE for details.
# SPDX-License-Identifier: GPL-3.0-only

import copy
import json
import multiprocessing as mp
import os
import re
import signal
import subprocess
import sys
import time

num_workloads = int(sys.argv[1])
num_processes = int(sys.argv[2])
root_path_to_verismith = sys.argv[3]

# Find the path to verismith
from pathlib import Path
def find_file_in_subdirectories(root_dir, filename):
    root_path = Path(root_dir)
    matches = []
    for path in root_path.rglob(filename):
        matches.append(path)
    assert len(matches) == 1, f"Expected to find exactly one file with name {filename} in the subdirectories of {root_dir}, but found {len(matches)} files."
    return matches[0]
path_to_verismith = find_file_in_subdirectories(root_path_to_verismith, "verismith")

MAX_NUM_CELLS = 10000
TIMEOUT_SECONDS = 5 # Might change to 900 like in the original paper

TMP_DIR = "tmp"
os.makedirs(TMP_DIR, exist_ok=True)
os.makedirs(os.path.join(TMP_DIR, "designs"), exist_ok=True)
os.makedirs(os.path.join(TMP_DIR, "prepared_for_equiv"), exist_ok=True)
os.makedirs(os.path.join(TMP_DIR, "logs"), exist_ok=True)

# Generates a list of modules
def gen_design():
    # Generate a design using the command `cabal run verismith generate`
    design = subprocess.run([f"{path_to_verismith} generate"], shell=True, stdout=subprocess.PIPE).stdout.decode('utf-8')
    return design

def synthesize_design(verilog_input_filepath, verilog_output_filepath, log_name):
    # Modify the environment
    curr_env = os.environ.copy()
    curr_env["VERILOG_INPUT"] = verilog_input_filepath
    curr_env["VERILOG_OUTPUT"] = verilog_output_filepath
    curr_env["TOP_MODULE"] = "top"

    # Copy the verilog file to the output file
    subprocess.run([f"yosys -c synthesize.ys.tcl -l {log_name}"], shell=True, env=curr_env, stdout=subprocess.PIPE)

# The modules generated by Verismith, except the top module, have names like module\d+
def rename_all_verismith_modules(verilog_str):
    # append all module\d+ with module\d+_replacedname
    return re.sub(r"(module\d+)", r"\1_replacedname", verilog_str)

def __get_num_cells(design_filepath):
    # We use Yosys for this.
    curr_env = os.environ.copy()
    curr_env["VERILOG_INPUT"] = design_filepath
    curr_env["TOP_MODULE"] = "top"

    stdout = subprocess.run(f"yosys -c stats.ys.tcl", shell=True, env=curr_env, stdout=subprocess.PIPE).stdout.decode('utf-8')

    # Find the last occurrence of `Number of cells:`, starting from behind
    stdout_lines = list(map(lambda s: s.strip(), stdout.split("\n")))
    for line_id in range(len(stdout_lines) - 1, -1, -1):
        if stdout_lines[line_id].startswith("Number of cells:"):
            return int(stdout_lines[line_id].split(":")[1].strip())

    raise Exception(f"Could not find the number of cells in the stdout for filepath: {design_filepath}")

# Return True iff equivalent
def check_equiv(first_filepath, second_filepath):
    # Replace the top module name in the verilog files
    with open(first_filepath, "r") as f:
        first_verilog = f.read()
    with open(second_filepath, "r") as f:
        second_verilog = f.read()
    design_hash = hex(abs(hash(first_verilog)))[2:]
    target_first_filepath = os.path.join(TMP_DIR, "prepared_for_equiv", f"equiv_{design_hash}_0.v")
    first_file_transformed_content = first_verilog.replace("module top", "module top_first")
    del first_verilog
    with open(target_first_filepath, "w") as f:
        f.write(first_file_transformed_content)
    target_second_filepath = os.path.join(TMP_DIR, "prepared_for_equiv", f"equiv_{design_hash}_1.v")
    # Also rename the modules of the second file
    second_file_transformed_content = rename_all_verismith_modules(second_verilog)
    del second_verilog
    second_file_transformed_content = second_file_transformed_content.replace("module top", "module top_second")
    with open(target_second_filepath, "w") as f:
        f.write(second_file_transformed_content)

    # Find the input wires in the top of the first
    # First, find the module top_first
    first_file_lines = first_file_transformed_content.split("\n")
    top_first_line_id = None
    for i, line in enumerate(first_file_lines):
        if line.strip().startswith("module top_first"):
            top_first_line_id = i
            break
    # Until a line that starts with endmodule, find all wires that are inputs
    input_wire_lines = []
    for line in first_file_lines[top_first_line_id:]:
        stripped_line = line.strip()
        if stripped_line.startswith("endmodule"):
            break
        if stripped_line.startswith("input"):
            input_wire_lines.append(line.strip())

    
    input_wire_dimensions = []
    input_wire_names = []
    for input_line in input_wire_lines:
        # Find the name of the wire
        splitted_line = input_line.split(" ")
        wire_dimensions = copy.copy(splitted_line[:-1])
        wire_name = splitted_line[-1][:-1] # The last [:-1] is to remove the semicolon
        input_wire_dimensions.append(wire_dimensions)
        input_wire_names.append(wire_name)

    # Also get the output wire
    output_wire_line_id = None
    for i, line in enumerate(first_file_lines):
        if line.strip().startswith("output wire"):
            output_wire_line_id = i
            break
    assert output_wire_line_id is not None, f"Could not find the output wire line in the first file {target_first_filepath}"

    output_wire_line = first_file_lines[output_wire_line_id].strip()
    assert output_wire_line.startswith("output wire") and output_wire_line.endswith("y;"), f"Output wire line is {output_wire_line} but should be 'output wire ... y;"

    # Create the new toplevel
    new_top_lines = []
    new_top_lines.append(f"module top_equiv (y_a, y_b, {', '.join(input_wire_names)});")
    new_top_lines.append(output_wire_line.replace('y;', 'y_a;'))
    new_top_lines.append(output_wire_line.replace('y;', 'y_b;'))
    new_top_lines += input_wire_lines
    new_top_lines.append(f"top_first top_first_inst (.y(y_a), {', '.join(map(lambda s: '.'+s, input_wire_names))});")
    new_top_lines.append(f"top_second top_second_inst (.y(y_b), {', '.join(map(lambda s: '.'+s, input_wire_names))});")
    new_top_lines.append("endmodule")

    target_top_filepath = os.path.join(TMP_DIR, "prepared_for_equiv", f"equiv_{design_hash}_top.v")
    with open(target_top_filepath, "w") as f:
        f.write("\n".join(new_top_lines))

    curr_env = os.environ.copy()
    curr_env["VERILOG_INPUT_FIRST"]  = target_first_filepath
    curr_env["VERILOG_INPUT_SECOND"] = target_second_filepath
    curr_env["VERILOG_INPUT_TOP"]    = target_top_filepath

    cmd = f"yosys -c equivtest.ys.tcl"

    # Measurement

    process = subprocess.Popen([cmd], shell=True, stdout=subprocess.PIPE, env=curr_env, stderr=subprocess.PIPE)

    start_time = time.time()
    try:
        # Wait for the specified timeout period
        stdout, stderr = process.communicate(timeout=TIMEOUT_SECONDS)
        end_time = time.time()
        return end_time - start_time
    except subprocess.TimeoutExpired:
        # If timeout occurs, kill the process using SIGKILL
        print("TIMEOUT!")
        os.kill(process.pid, signal.SIGKILL)
        print(f"Process killed after {TIMEOUT_SECONDS} seconds timeout.")
        return None

manager = mp.Manager()
lock = manager.Lock()
set_of_design_hashes = manager.list()

def test_new_design(set_of_design_hashes, lock, workload_id):
    num_cells = None
    while num_cells is None or num_cells > MAX_NUM_CELLS:
        design = gen_design()
        design_hash = f"{hex(abs(hash(design)))[2:]:0>16}"
        with lock:
            if design_hash in set_of_design_hashes:
                print(f"Skipping duplicate design with hash {design_hash}")
                continue
            set_of_design_hashes.append(design_hash)

        path_to_design = os.path.join(TMP_DIR, "designs", f"design_{workload_id}_{design_hash}.v")
        with open(path_to_design, "w") as f:
            f.write(design)
        num_cells = __get_num_cells(path_to_design)

    # Synthesize the design
    path_to_design_synthesized = os.path.join(TMP_DIR, "designs", f"design_synthesized_{workload_id}_{design_hash}.v")
    synthesize_design(path_to_design, path_to_design_synthesized, os.path.join(TMP_DIR, "logs", f"synth_{workload_id}_{design_hash}.log"))

    check_duration = check_equiv(path_to_design, path_to_design_synthesized)
    if check_duration is not None:
        print(f"check_duration: {check_duration: >7.2f}, num_cells: {num_cells:>4}, hash: {design_hash}")
    else:
        print(f"check_duration: TIMEOUT, num_cells: {num_cells:>4}, hash: {design_hash}")
        # check_duration:>4.f}, num_cells: {num_cells:>4}, hash: {design_hash}")
    if check_duration is None:
        check_duration = TIMEOUT_SECONDS
    return num_cells, check_duration

with mp.Pool(processes=num_processes) as pool:
    results = pool.starmap(test_new_design, ((set_of_design_hashes, lock, i) for i in range(num_workloads)))

# Remove the temporary directory
subprocess.run([f"rm -rf {TMP_DIR}"], shell=True)

with open("performance_results.json", "w") as f:
    json.dump(results, f)
