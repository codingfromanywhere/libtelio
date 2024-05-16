import json
import os
import re
import subprocess
import requests
import zipfile
from datetime import datetime


# This script is used to download the latest tagged build artifacts from the GitLab CI pipeline.
class ArtifactsDownloader:
    def __init__(
        self, target_os, target_arch, token, download_dir="./", tag_prefix="nightly"
    ):
        self.target_os = target_os
        self.target_arch = target_arch
        self.token = token
        self.download_dir = download_dir
        self.tag_prefix = tag_prefix

    def download(self) -> bool:
        _, tag_msg = self._get_latest_tag(self.tag_prefix)

        if tag_msg:
            tag_json = json.loads(tag_msg)
            return self._get_pipeline_build_artifacts(
                tag_json["pipeline_id"],
                self.download_dir,
                self.target_arch,
                self.target_os,
            )
        else:
            print(f"No {self.tag_prefix} tag found.")

        return False

    def _extract_date(self, tag, tag_prefix):
        """Extract the date from the tag name."""
        date_char_count = 6

        if tag_prefix == "main":
            date_char_count = 10

        date_str = re.search(f"{tag_prefix}-([0-9]{{{date_char_count}}})", tag).group(1)
        return date_str

    def _get_latest_tag(self, tag_prefix):
        """Find the latest tag with the given prefix."""
        subprocess.run(
            ["git", "-C", self.download_dir, "fetch", "--tags", "--quiet"], check=True
        )

        tags = (
            subprocess.check_output(
                ["git", "-C", self.download_dir, "tag", "--sort=-creatordate"]
            )
            .decode()
            .splitlines()
        )
        tags = [tag for tag in tags if tag.startswith(f"{tag_prefix}-")]

        latest_tag = None
        latest_date = None

        for tag in tags:
            date_str = self._extract_date(tag, tag_prefix)
            date = datetime.strptime(
                date_str, "%y%m%d" if len(date_str) == 6 else "%y%m%d%H%M"
            )
            if latest_date is None or date > latest_date:
                latest_tag = tag
                latest_date = date

        if latest_tag:
            message = (
                subprocess.check_output(
                    ["git", "-C", self.download_dir, "tag", "-l", "-n1", latest_tag]
                )
                .decode()
                .strip()
                .split(" ", 1)[1]
            )
            return latest_tag, message

        return None, None

    def _get_remote_path(self) -> str:
        LIBTELIO_BUILD_PROJECT_ID = 6299
        libtelio_env_sec_gitlab_repository = os.environ.get(
            "LIBTELIO_ENV_SEC_GITLAB_REPOSITORY", None
        )

        if libtelio_env_sec_gitlab_repository is None:
            raise ValueError("LIBTELIO_ENV_SEC_GITLAB_REPOSITORY not set.")

        return f"https://{libtelio_env_sec_gitlab_repository}/api/v4/projects/{LIBTELIO_BUILD_PROJECT_ID}"

    def _get_api(self, path, timeout=300):
        with requests.get(
            self._get_remote_path() + path,
            headers={"PRIVATE-TOKEN": self.token if self.token else ""},
            timeout=timeout,
        ) as request:
            request.raise_for_status()
            response_string = request.content.decode("utf-8")
            return response_string

    def _get_artifacts(self, path_to_save, job, timeout=300, unzip=False):
        full_path = path_to_save + job["artifacts_file"]["filename"]

        print("Getting artficats for ", job["name"], ", filename: ", full_path)

        r = requests.get(
            self._get_remote_path() + "/jobs/" + str(job["id"]) + "/artifacts",
            headers={"PRIVATE-TOKEN": self.token if self.token else ""},
            timeout=timeout,
        )
        with open(str(full_path), "wb") as f:
            f.write(r.content)

        with zipfile.ZipFile(full_path, "r") as zip_ref:
            zip_ref.extractall(path_to_save)

    def _get_pipeline_build_artifacts(
        self, pipeline_id, path_to_save, target_arch, target_os
    ):
        for job in json.loads(
            self._get_api(
                (
                    f"/pipelines/{pipeline_id}/jobs?per_page=100&include_retried=true&scope=success"
                )
            )
        ):
            if job["stage"] == "build":
                if target_os == "uniffi" and job["name"] == "uniffi-bindings":
                    self._get_artifacts(path_to_save, job)
                    return True
                else:
                    if (
                        target_os in job["name"] if target_os is not None else True
                    ) and target_arch in job["name"]:
                        self._get_artifacts(path_to_save, job, unzip=True)
                        return True

        return False
