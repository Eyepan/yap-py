#!/usr/bin/env python3

import json
import logging
import os
import pickle
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import semantic_version

import requests

from errors import CacheError, MetadataError, NetworkError

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration setup
CONFIG = {"registry": "https://registry.npmjs.org/"}
METADATA_DOWNLOADED_PACKAGES = set()
metadata_lock = threading.Lock()
TARBALL_DOWNLOADED_PACKAGES = set()
tarball_lock = threading.Lock()


def process_npmrc(file_path):
    if not file_path.exists():
        return
    with open(file_path, "r") as f:
        for line in f:
            if line.startswith("//"):
                registry, authToken = line.split(":")
                CONFIG["registry"] = "https:" + registry
                CONFIG["authToken"] = authToken.split("=")[1].strip()
            else:
                configName, configValue = line.split("=")
                CONFIG[configName.strip()] = configValue.strip()


if not CONFIG["registry"].endswith("/"):
    CONFIG["registry"] += "/"

# Setup store and cache directories
STORE_DIR = Path.cwd() / ".yap_store"
CACHE_DIR = STORE_DIR / ".yap_cache"
NODE_MODULES_DIR = Path.cwd() / "node_modules"

for path in [STORE_DIR, CACHE_DIR, NODE_MODULES_DIR]:
    path.mkdir(parents=True, exist_ok=True)


# Cache management
def set_to_metadata_cache(name: str, contents: any):
    cache_file = CACHE_DIR / name.replace("/", "_")
    try:
        with cache_file.open("wb") as f:
            pickle.dump(contents, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        raise CacheError(f"Error saving to cache for '{name}': {e}")


def get_from_metadata_cache(name: str):
    cache_file = CACHE_DIR / name.replace("/", "_")
    if cache_file.exists():
        try:
            with cache_file.open("rb") as f:
                return pickle.load(f)
        except Exception as e:
            raise CacheError(f"Error reading from cache for '{name}': {e}")
    return None


# HTTP session
session = requests.Session()
if "authToken" in CONFIG:
    session.headers["Authorization"] = f"Bearer {CONFIG['authToken']}"


def fetch_package_metadata(name: str) -> dict:
    cached_metadata = get_from_metadata_cache(name)
    if cached_metadata:
        return cached_metadata

    package_url = f"{CONFIG['registry']}{name}"
    session.headers["Accept"] = (
        "application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8, */*"
    )
    response = session.get(package_url)
    if response.status_code != 200:
        raise NetworkError(
            f"Failed to fetch {package_url}: {response.status_code} {response.reason}"
        )

    try:
        package_details = response.json()
        set_to_metadata_cache(name, package_details)
        return package_details
    except ValueError as e:
        raise MetadataError(f"Error parsing JSON from {package_url}: {e}")


def load_lock_file(lock_file_path):
    if lock_file_path.exists():
        logger.info("Lockfile found, skipping resolution")
        with lock_file_path.open("rb") as f:
            return pickle.load(f)
    return []


def save_lock_file(lock_file_path, lock_file_details):
    with lock_file_path.open("wb") as f:
        pickle.dump(lock_file_details, f)


def load_package_json(package_json_path):
    if not package_json_path.exists():
        logger.error("package.json not found")
        exit(1)
    with package_json_path.open("r") as f:
        data = json.load(f)
        return {
            **data.get("dependencies", {}),
            **data.get("devDependencies", {}),
            **data.get("peerDependencies", {}),
        }


def resolve_dependency_and_queue_urls(dependencies, lock_file_details):
    futures = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        for package_name, version in dependencies.items():
            futures.append(
                executor.submit(
                    resolve_single_dependency, package_name, version, lock_file_details
                )
            )
        for future in futures:
            future.result()


def resolve_version(wanted_version: str, available_versions):
    wanted = semantic_version.NpmSpec(wanted_version)
    selected_version = wanted.select(
        [semantic_version.Version(v) for v in available_versions]
    )
    return selected_version.__str__()


def resolve_single_dependency(package_name, version, lock_file_details):
    with metadata_lock:
        if package_name in METADATA_DOWNLOADED_PACKAGES:
            return
        METADATA_DOWNLOADED_PACKAGES.add(package_name)

    if version.startswith(("git+", "git:")):
        return

    logger.info(f"RESOLVING: {package_name} {version}")

    if version.startswith("npm:"):
        new_package_name = version[4:]
        package_name, version = safe_package_details(new_package_name)

    package_metadata = fetch_package_metadata(package_name)

    available_versions = package_metadata["versions"].keys()
    resolved_version = resolve_version(version, available_versions)
    if not resolved_version:
        raise MetadataError(f"Could not resolve version for {package_name} {version}")
    version_metadata = package_metadata["versions"][resolved_version]
    version_metadata["name"] = package_name

    resolve_dependency_and_queue_urls(
        version_metadata.get("dependencies", {}), lock_file_details
    )

    tarball_url = version_metadata["dist"]["tarball"]
    lock_file_details.append(
        {
            "url": tarball_url,
            "name": package_name,
            "version": resolved_version,
            "dependencies": version_metadata.get("dependencies", {}),
        }
    )


def safe_package_name(name: str):
    return name.replace("/", "_")


def safe_package_details(package_str):
    if package_str.startswith("@"):
        package_name = f"@{package_str.split("@")[1]}"
        version = package_str.split("@")[2]
        return package_name, version
    else:
        package_name = package_str.split("@")[0]
        version = package_str.split("@")[1]
        return package_name, version


import os
import tarfile
from pathlib import Path


def download_and_extract_package(package):
    with tarball_lock:
        if package["name"] in TARBALL_DOWNLOADED_PACKAGES:
            return
        TARBALL_DOWNLOADED_PACKAGES.add(package["name"])

    tarball_path = (
        STORE_DIR / f"{safe_package_name(package['name'])}@{package['version']}.tgz"
    )
    package_path = (
        STORE_DIR / f"{safe_package_name(package['name'])}@{package['version']}"
    )
    if package_path.exists():
        logger.info(f"Package already exists: {package['name']}")
        return

    logger.info(f"DOWNLOADING {package['name']}")
    response = session.get(package["url"])
    if response.status_code != 200:
        raise NetworkError(
            f"Failed to fetch {package['url']}: {response.status_code} {response.reason}"
        )

    with tarball_path.open("wb") as f:
        f.write(response.content)

    with tarfile.open(tarball_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.startswith("package/"):
                member.name = member.name[len("package/") :]
                tar.extract(member, path=package_path)

    os.remove(tarball_path)


def create_symlink(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_file():
        destination.unlink()
    os.symlink(source, destination)


def symlink_dependencies(package):
    package_dir = NODE_MODULES_DIR / package["name"]
    for dep_name, dep_version in package["dependencies"].items():
        source_path = NODE_MODULES_DIR / dep_name
        dest_path = package_dir / "node_modules" / dep_name
        logger.info(f"SYMLINKING {source_path} -> {dest_path}")
        create_symlink(source_path, dest_path)


def symlink_to_root(package):
    source_path = (
        NODE_MODULES_DIR
        / ".yap"
        / f"{safe_package_name(package['name'])}@{package['version']}"
    )
    dest_path = NODE_MODULES_DIR / package["name"]
    logger.info(f"SYMLINKING {source_path} -> {dest_path}")
    create_symlink(source_path, dest_path)


def run_postinstall_scripts(lock_file_details):
    for package in lock_file_details:
        package_dir = NODE_MODULES_DIR / package["name"]
        postinstall_script = package_dir / "node_modules" / ".bin" / "postinstall"
        if postinstall_script.exists():
            logger.info(f"Running postinstall script for {package['name']}")
            os.system(f"cd {package_dir} && {postinstall_script}")


def main():
    lock_file_path = Path.cwd() / "yap.lock"
    lock_file_details = load_lock_file(lock_file_path)

    if not lock_file_details:
        package_json_path = Path.cwd() / "package.json"
        dependencies = load_package_json(package_json_path)
        resolve_dependency_and_queue_urls(dependencies, lock_file_details)
        save_lock_file(lock_file_path, lock_file_details)
        logger.info(f"Downloaded: {len(METADATA_DOWNLOADED_PACKAGES)} metadatas")

    with ThreadPoolExecutor(max_workers=10) as executor:
        executor.map(download_and_extract_package, lock_file_details)
    logger.info(
        f"Downloaded and extracted: {len(TARBALL_DOWNLOADED_PACKAGES)} tarballs"
    )

    logger.info("Hardlinking packages")
    for package in lock_file_details:
        package_dir = (
            STORE_DIR / f"{safe_package_name(package['name'])}@{package['version']}"
        )
        target_package_dir = (
            NODE_MODULES_DIR
            / ".yap"
            / f"{safe_package_name(package['name'])}@{package['version']}"
        )
        target_package_dir.mkdir(parents=True, exist_ok=True)
        for root, dirs, files in os.walk(package_dir):
            for file in files:
                source = Path(root) / file
                dest = target_package_dir / source.relative_to(package_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    dest.unlink()
                os.link(source, dest)
    logger.info("Packages hardlinked successfully.")

    for package in lock_file_details:
        symlink_to_root(package)
        create_symlink(
            NODE_MODULES_DIR / package["name"],
            NODE_MODULES_DIR / package["name"] / "node_modules" / package["name"],
        )
        symlink_dependencies(package)

    logger.info("Packages installed successfully.")
    run_postinstall_scripts(lock_file_details)
    logger.info("Postinstall scripts run successfully.")


if __name__ == "__main__":
    main()
