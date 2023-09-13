import os.path
import subprocess
import shutil
import sys
import time
import fcntl
import signal
import logging
import threading
import socket
from ray._private.ray_process_reaper import SIGTERM_GRACE_PERIOD_SECONDS
from ray.util.spark.cluster_init import (
    RAY_ON_SPARK_COLLECT_LOG_TO_PATH,
    START_RAY_WORKER_NODE,
)
from ray.util.spark.databricks_hook import global_mode_enabled


# Spark on ray implementation does not directly invoke `ray start ...` script to create
# ray node subprocess, instead, it creates a subprocess to run this
# `ray.util.spark.start_ray_node` module, and in this module it invokes `ray start ...`
# script to start ray node, the purpose of `start_ray_node` module is to set up a
# SIGTERM handler for cleaning ray temp directory when ray node exits.
# When spark driver python process dies, or spark python worker dies, because they
# registered the PR_SET_PDEATHSIG signal, so OS will send a SIGTERM signal to its
# children processes, so `start_ray_node` subprocess will receive a SIGTERM signal and
# the SIGTERM handler will do cleanup work.


_logger = logging.getLogger(__name__)


if __name__ == "__main__":
    arg_list = sys.argv[1:]

    collect_log_to_path = os.environ[RAY_ON_SPARK_COLLECT_LOG_TO_PATH]

    temp_dir_arg_prefix = "--temp-dir="
    temp_dir = None

    for arg in arg_list:
        if arg.startswith(temp_dir_arg_prefix):
            temp_dir = arg[len(temp_dir_arg_prefix) :]

    if temp_dir is None:
        raise ValueError("Please explicitly set --temp-dir option.")

    temp_dir = os.path.normpath(temp_dir)

    def running_on_worker_node():
        return os.environ.get(START_RAY_WORKER_NODE, "false").lower() == "true"

    # Clean up the temp dir on head node for global mode
    # Worker nodes will clean up their temp dirs when they exit.
    if (
        global_mode_enabled()
        and not running_on_worker_node()
        and os.path.exists(temp_dir)
    ):
        shutil.rmtree(temp_dir, ignore_errors=True)

    ray_cli_cmd = "ray"

    lock_file = temp_dir + ".lock"
    lock_fd = os.open(lock_file, os.O_RDWR | os.O_CREAT | os.O_TRUNC)

    # Mutilple ray nodes might start on the same machine, and they are using the
    # same temp directory, adding a shared lock representing current ray node is
    # using the temp directory.
    fcntl.flock(lock_fd, fcntl.LOCK_SH)
    process = subprocess.Popen([ray_cli_cmd, "start", *arg_list], text=True)
    # This makes sure that ray node has created the temp dir for ray node session.
    for i in range(4):
        time.sleep(5)
        if os.path.exists(os.path.join(temp_dir, "session_latest")):
            break
    ray_session_dir = os.readlink(os.path.join(temp_dir, "session_latest"))

    clean_temp_dir_lock = threading.RLock()

    def try_clean_temp_dir_at_exit():
        with clean_temp_dir_lock:
            try:
                # Wait for a while to ensure the children processes of the ray node all
                # exited.
                time.sleep(SIGTERM_GRACE_PERIOD_SECONDS + 0.5)
                if process.poll() is None:
                    # "ray start ..." command process is still alive. Force to kill it.
                    process.kill()

                # Release the shared lock, representing current ray node does not
                # use the temp dir.
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

                try:
                    # acquiring exclusive lock to ensure copy logs and
                    # removing dir safely.
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_acquired = True
                except BlockingIOError:
                    # The file has active shared lock or exclusive lock, representing
                    # there are other ray nodes running, or other node running cleanup
                    # temp-dir routine. skip cleaning temp-dir, and skip copy logs to
                    # destination directory as well.
                    lock_acquired = False

                if lock_acquired:
                    # This is the final terminated ray node on current spark worker,
                    # start copy logs (including all local ray nodes logs)
                    # to destination.
                    if collect_log_to_path:
                        try:
                            copy_log_dest_path = os.path.join(
                                collect_log_to_path,
                                os.path.basename(temp_dir) + "-logs",
                                socket.gethostname(),
                            )
                            shutil.copytree(
                                os.path.join(ray_session_dir, "logs"),
                                copy_log_dest_path,
                            )
                        except Exception as e:
                            _logger.warning(
                                "Collect logs to destination directory failed, "
                                f"error: {repr(e)}."
                            )

                    # Start cleaning the temp-dir,
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                # swallow any exception.
                pass
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def check_parent_alive() -> None:
        orig_parent_id = os.getppid()
        while True:
            time.sleep(0.5)
            if os.getppid() != orig_parent_id:
                process.terminate()
                try_clean_temp_dir_at_exit()
                os._exit(143)

    # When global mode is enabled and head node is started, parent process could be
    # detached so we don't kill the process when parent process died.
    # But we should always check parent alive for worker nodes, as spark application
    # only kills worker processes but not subprocesses within them.
    if not global_mode_enabled() or running_on_worker_node():
        threading.Thread(target=check_parent_alive, daemon=True).start()

    try:

        def sighup_handler(*args):
            pass

        # When spark application is terminated, this process will receive
        # SIGHUP (comes from pyspark application termination).
        # Ignore the SIGHUP signal, because in this case,
        # `check_parent_alive` will capture parent process died event
        # and execute killing node and cleanup routine
        # but if we enable default SIGHUP handler, it will kill
        # the process immediately and it causes `check_parent_alive`
        # have no time to exeucte cleanup routine.
        signal.signal(signal.SIGHUP, sighup_handler)

        def sigterm_handler(*args):
            process.terminate()
            try_clean_temp_dir_at_exit()
            os._exit(143)

        signal.signal(signal.SIGTERM, sigterm_handler)
        ret_code = process.wait()
        try_clean_temp_dir_at_exit()
        sys.exit(ret_code)
    except Exception:
        try_clean_temp_dir_at_exit()
        raise
