"""
Altermagnet Screener — Streamlit app (hosted-deployment version)
------------------------------------------------------------------
Upload VASP structure files, and this app drives `amcheck` interactively
(answering its spin prompts) across every u/d spin combination for
magnetic elements, flagging any structure where at least one combination
reports `Altermagnet? True`.

Designed to run on Streamlit Community Cloud (or any host) — no local
filesystem access required, everything happens through file upload /
zip download.
"""

import itertools
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

import streamlit as st

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SPECIAL_ELEMENTS = {"Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Mo", "Ru"}
MAX_N = 26
ATOM_COUNT_HARD_LIMIT = 24

ELEMENT_RE = re.compile(r"Orbit of (\w+) atoms at positions:")
ATOM_RE = re.compile(r"\d+ \(\d+\) \[\s*[-?\d.eE]+\s+[-?\d.eE]+\s+[-?\d.eE]+\s*\]")
ALTERMAGNET_RE = re.compile(r"Altermagnet\?\s*(True|False)")
SPIN_PROMPT = "Type spin (u, U, d, D, n, N, nn or NN) for each of them"
PRIMITIVE_CELL_PROMPT = "Do you want to use it instead? (Y/n)"


# --------------------------------------------------------------------------
# amcheck availability
# --------------------------------------------------------------------------

def check_amcheck_installed() -> bool:
    try:
        subprocess.run(
            ["amcheck", "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# u/d sequence generation
# --------------------------------------------------------------------------

def flip(seq: str) -> str:
    return "".join("d" if c == "u" else "u" for c in seq)


def generate_combinations(n: int) -> list:
    if n % 2 != 0:
        return []
    result, seen = [], set()

    def backtrack(path, u_count, d_count):
        if len(path) == n:
            seq = "".join(path)
            flipped = flip(seq)
            if seq not in seen and flipped not in seen:
                seen.add(seq)
                result.append(" ".join(seq))
            return
        if u_count < n // 2:
            backtrack(path + ["u"], u_count + 1, d_count)
        if d_count < n // 2:
            backtrack(path + ["d"], u_count, d_count + 1)

    backtrack([], 0, 0)
    result.sort()
    return result


@st.cache_data(show_spinner=False)
def build_sequence_map(max_n: int) -> dict:
    return {n: generate_combinations(n) for n in range(2, max_n + 1, 2)}


# --------------------------------------------------------------------------
# Interactive amcheck runners
# --------------------------------------------------------------------------

def run_nn_amcheck(vasp_file_path: str, log_lines: list) -> tuple:
    element = None
    atom_count = 0
    element_array, atom_count_array = [], []

    proc = subprocess.Popen(
        ["amcheck", vasp_file_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )

    for line in proc.stdout:
        line = line.rstrip("\n")
        log_lines.append(line)

        if PRIMITIVE_CELL_PROMPT in line:
            proc.stdin.write("Y\n"); proc.stdin.flush()

        m = ELEMENT_RE.search(line)
        if m:
            element = m.group(1)
            atom_count = 0

        if ATOM_RE.search(line):
            atom_count += 1

        if SPIN_PROMPT in line:
            element_array.append(element)
            atom_count_array.append(atom_count)
            if element in SPECIAL_ELEMENTS:
                options = ["u", "d"]
                response = [options[i % 2] for i in range(atom_count)]
            else:
                response = ["nn"]
            response_str = " ".join(response)
            log_lines.append(f"Sending response: {response_str}")
            proc.stdin.write(response_str + "\n"); proc.stdin.flush()

    proc.stdin.close()
    proc.wait()
    return element_array, atom_count_array


def run_amcheck(vasp_file_path: str, input_array: list, log_lines: list):
    index = 0
    altermagnet_result = None

    proc = subprocess.Popen(
        ["amcheck", vasp_file_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )

    for line in proc.stdout:
        line = line.rstrip("\n")
        log_lines.append(line)

        if PRIMITIVE_CELL_PROMPT in line:
            proc.stdin.write("Y\n"); proc.stdin.flush()

        m = ALTERMAGNET_RE.search(line)
        if m:
            altermagnet_result = m.group(1)

        if SPIN_PROMPT in line:
            response_str = input_array[index]
            index += 1
            log_lines.append(f"Sending response: {response_str}")
            proc.stdin.write(response_str + "\n"); proc.stdin.flush()

    proc.stdin.close()
    proc.wait()
    log_lines.append(f"Altermagnet Result: {altermagnet_result}")
    return altermagnet_result


def generate_input_combinations(vasp_file_path, arr, log_lines, progress_cb=None):
    total = 0
    true_count = 0
    combos = list(itertools.product(*arr)) if arr else [()]
    for combo in combos:
        total += 1
        output = run_amcheck(vasp_file_path, list(combo), log_lines)
        if output == "True":
            true_count += 1
        if progress_cb:
            progress_cb(total, len(combos), true_count)
    return total, true_count


def process_file(vasp_file_path, true_files_dir, sequence_map, log_lines, progress_cb=None):
    element_array, atom_count_array = run_nn_amcheck(vasp_file_path, log_lines)
    log_lines.append(f"Count of element is {element_array} {atom_count_array}")

    element_input_array = []
    for element, atom_count in zip(element_array, atom_count_array):
        if element in SPECIAL_ELEMENTS:
            if atom_count > ATOM_COUNT_HARD_LIMIT:
                msg = "Atom Count is Greater than 24. Try more combinations."
                log_lines.append(msg)
                return {
                    "File": os.path.basename(vasp_file_path),
                    "Status": "Skipped",
                    "Reason": msg,
                    "Combinations tried": 0,
                    "Altermagnet? True count": 0,
                    "Flagged": False,
                }
            element_input_array.append(sequence_map.get(atom_count, []))
        else:
            element_input_array.append(["nn"])

    total, true_count = generate_input_combinations(
        vasp_file_path, element_input_array, log_lines, progress_cb
    )

    result = {
        "File": os.path.basename(vasp_file_path),
        "Status": "Done",
        "Reason": "",
        "Combinations tried": total,
        "Altermagnet? True count": true_count,
        "Flagged": true_count > 0,
    }

    if true_count > 0:
        dest = os.path.join(true_files_dir, os.path.basename(vasp_file_path))
        shutil.copy(vasp_file_path, dest)

    return result


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

st.set_page_config(page_title="Altermagnet Screener", page_icon="🧲", layout="wide")

st.markdown(
    """
    <style>
    .block-container {padding-top: 2.2rem;}
    div[data-testid="stMetricValue"] {font-size: 1.6rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🧲 Altermagnet Screener")
st.caption(
    "Upload VASP structure files. This app runs `amcheck` across every u/d spin "
    "combination for magnetic elements and flags any structure with at least one "
    "combination where **Altermagnet? True**."
)

amcheck_ok = check_amcheck_installed()
if not amcheck_ok:
    st.error(
        "⚠️ `amcheck` isn't available on this server. Add `amcheck` to your "
        "`requirements.txt` and redeploy — see the deployment notes at the bottom of this page."
    )

if "results" not in st.session_state:
    st.session_state.results = []
if "full_log" not in st.session_state:
    st.session_state.full_log = []
if "true_files_dir" not in st.session_state:
    st.session_state.true_files_dir = None

st.divider()

uploaded_files = st.file_uploader(
    "Drop VASP structure files here",
    accept_multiple_files=True,
    help="You can select or drag in multiple files at once.",
)

run_col, clear_col = st.columns([1, 1])
run_clicked = run_col.button("▶ Run analysis", type="primary", disabled=not amcheck_ok)
clear_clicked = clear_col.button("🗑 Clear results")

if clear_clicked:
    st.session_state.results = []
    st.session_state.full_log = []
    st.session_state.true_files_dir = None
    st.rerun()

if run_clicked:
    if not uploaded_files:
        st.warning("Please upload at least one file first.")
        st.stop()

    st.session_state.results = []
    st.session_state.full_log = []

    work_dir = tempfile.mkdtemp(prefix="amcheck_uploads_")
    output_dir = tempfile.mkdtemp(prefix="amcheck_output_")
    true_files_dir = os.path.join(output_dir, "trueFiles")
    os.makedirs(true_files_dir, exist_ok=True)

    file_paths = []
    for uf in uploaded_files:
        dest = os.path.join(work_dir, uf.name)
        with open(dest, "wb") as fh:
            fh.write(uf.getbuffer())
        file_paths.append(dest)

    sequence_map = build_sequence_map(MAX_N)

    st.subheader("Progress")
    overall_progress = st.progress(0.0, text="Starting...")
    file_progress = st.progress(0.0, text="")
    with st.expander("Live log", expanded=False):
        log_box = st.empty()
    results_box = st.empty()

    all_log_lines = []

    for i, path in enumerate(file_paths):
        overall_progress.progress(
            i / len(file_paths),
            text=f"Processing file {i + 1}/{len(file_paths)}: {os.path.basename(path)}",
        )
        file_log = []

        def progress_cb(done, total, trues, _path=path):
            pct = done / total if total else 1.0
            file_progress.progress(
                pct,
                text=f"{os.path.basename(_path)}: combination {done}/{total} "
                     f"({trues} True so far)",
            )
            if done % 5 == 0 or done == total:
                log_box.code("\n".join(file_log[-200:]), language="text")

        result = process_file(path, true_files_dir, sequence_map, file_log, progress_cb)
        st.session_state.results.append(result)
        all_log_lines.extend(file_log)
        all_log_lines.append(f"File Number Processed: {i}")
        results_box.dataframe(st.session_state.results, use_container_width=True)

    overall_progress.progress(1.0, text="Done!")
    file_progress.progress(1.0, text="")

    st.session_state.full_log = all_log_lines
    st.session_state.true_files_dir = true_files_dir
    shutil.rmtree(work_dir, ignore_errors=True)
    st.rerun()

# --------------------------------------------------------------------------
# Results display
# --------------------------------------------------------------------------

if st.session_state.results:
    st.divider()
    st.subheader("Results")

    n_files = len(st.session_state.results)
    n_flagged = sum(1 for r in st.session_state.results if r.get("Flagged"))
    m1, m2, m3 = st.columns(3)
    m1.metric("Files processed", n_files)
    m2.metric("Flagged as altermagnetic", n_flagged)
    m3.metric("Flag rate", f"{(n_flagged / n_files * 100):.0f}%" if n_files else "0%")

    st.dataframe(st.session_state.results, use_container_width=True)

    true_files_dir = st.session_state.true_files_dir
    dl_col1, dl_col2 = st.columns(2)

    if true_files_dir and os.path.isdir(true_files_dir) and os.listdir(true_files_dir):
        zip_path = os.path.join(tempfile.gettempdir(), "trueFiles.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for fname in os.listdir(true_files_dir):
                zf.write(os.path.join(true_files_dir, fname), arcname=fname)
        with open(zip_path, "rb") as zf:
            dl_col1.download_button(
                "⬇ Download flagged structures (trueFiles.zip)",
                data=zf.read(),
                file_name="trueFiles.zip",
                mime="application/zip",
            )
    else:
        dl_col1.info("No structures were flagged as altermagnetic.")

    dl_col2.download_button(
        "⬇ Download full run log",
        data="\n".join(st.session_state.full_log),
        file_name="amcheck_log.txt",
        mime="text/plain",
    )

    with st.expander("Full run log"):
        st.code("\n".join(st.session_state.full_log[-3000:]), language="text")
else:
    st.info("Upload files above and click **Run analysis** to begin.")

st.divider()
with st.expander("📦 First time deploying this? Read this"):
    st.markdown(
        """
1. Put this file (`streamlit_app.py`) and `requirements.txt` in a GitHub repo.
2. Go to **share.streamlit.io**, sign in with GitHub.
3. Click **New app**, pick your repo/branch, and set the main file path to
   `streamlit_app.py`.
4. Click **Deploy** — you'll get a public URL that opens this exact interface
   in a browser tab.
5. Make sure `requirements.txt` includes `amcheck` so the server installs it
   automatically (no manual setup needed).

Heads up: combinatorial screening on elements with many atoms can spawn a lot
of `amcheck` subprocess calls in sequence, so large structures may take a
while on free hosting tiers.
        """
    )
