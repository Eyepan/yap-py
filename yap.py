#!/usr/bin/env python3

import json
import os
import pickle
import tarfile
from pathlib import Path

import requests

from errors import CacheError, MetadataError, NetworkError
from resolver import resolve_version


# 0. Configuration setup
CONFIG = {"registry": "https://registry.npmjs.org/"}
METADATA_DOWNLOADED_PACKAGES = set()
TARBALL_DOWNLOADED_PACKAGES = set()


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


# 1. Check for existing lockfile
lock_file = Path.cwd() / "yap.lock"
if lock_file.exists():
    print("Lockfile found, skipping resolution")
    with lock_file.open("rb") as f:
        lock_file_details = pickle.load(f)
else:
    lock_file_details = []
    package_json = Path.cwd() / "package.json"
    if not package_json.exists():
        print("package.json not found")
        exit(1)

    # Load package.json dependencies
    with package_json.open("r") as f:
        data = json.load(f)
        dependencies = {
            **data.get("dependencies", {}),
            **data.get("devDependencies", {}),
            **data.get("peerDependencies", {}),
        }

    # Resolve dependencies and download metadata
    def resolve_dependency_and_queue_urls(dependencies):
        for package_name, version in dependencies.items():
            if package_name in METADATA_DOWNLOADED_PACKAGES:
                continue
            print(f"RESOLVING: {package_name} {version}")
            package_metadata = fetch_package_metadata(package_name)
            METADATA_DOWNLOADED_PACKAGES.add(package_name)

            # 3.5 Resolve version
            if version.startswith(("git+", "npm:", "git:")):
                continue
            available_versions = package_metadata["versions"].keys()
            resolved_version = resolve_version(version, available_versions)
            if not resolved_version:
                raise MetadataError(
                    f"Could not resolve version for {package_name} {version}"
                )
            package_metadata = package_metadata["versions"][resolved_version]

            # 4. Resolve dependencies recursively
            resolve_dependency_and_queue_urls(package_metadata.get("dependencies", {}))

            tarball_url = package_metadata["dist"]["tarball"]
            lock_file_details.append(
                {
                    "url": tarball_url,
                    "name": package_name,
                    "version": resolved_version,
                    "dependencies": package_metadata.get("dependencies", {}),
                }
            )

    resolve_dependency_and_queue_urls(dependencies)

    # Save lockfile
    with lock_file.open("wb") as f:
        pickle.dump(lock_file_details, f)


# 5. Download and extract packages
def download_and_extract_package(package):
    tarball_path = STORE_DIR / f"{package['name'].replace('/', '_')}.tgz"
    if (STORE_DIR / package["name"]).exists():
        print("Package already exists", package["name"])
        return

    print(f"Downloading {package['name']}")
    response = session.get(package["url"])
    if response.status_code != 200:
        raise NetworkError(
            f"Failed to fetch {package['url']}: {response.status_code} {response.reason}"
        )

    with tarball_path.open("wb") as f:
        f.write(response.content)

    with tarfile.open(tarball_path, "r:gz") as tar:
        tar.extractall(path=STORE_DIR / package["name"])

    os.remove(tarball_path)
    TARBALL_DOWNLOADED_PACKAGES.add(package["name"])


for package in lock_file_details:
    if package["name"] not in TARBALL_DOWNLOADED_PACKAGES:
        download_and_extract_package(package)

print("Hardlinking packages")
for package in lock_file_details:
    package_dir = STORE_DIR / package["name"]
    target_package_dir = NODE_MODULES_DIR / ".yap" / package["name"]
    target_package_dir.mkdir(parents=True, exist_ok=True)

    for root, dirs, files in os.walk(package_dir):
        for file in files:
            source = Path(root) / file
            dest = target_package_dir / source.relative_to(package_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            os.link(source, dest)
print("Packages hardlinked successfully.")


# 6. Install packages
def create_symlink(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    os.symlink(source, destination)


def symlink_dependencies(package_name: str, dependencies: dict):
    package_dir = NODE_MODULES_DIR / package_name
    for dep_name, dep_version in dependencies.items():
        source_path = NODE_MODULES_DIR / dep_name
        dest_path = package_dir / "node_modules" / dep_name
        print(f"Symlinking {source_path} -> {dest_path}")
        create_symlink(source_path, dest_path)


def symlink_to_root(package_name: str):
    source_path = NODE_MODULES_DIR / ".yap" / package_name
    dest_path = NODE_MODULES_DIR / package_name
    print(f"Symlinking {source_path} -> {dest_path}")
    create_symlink(source_path, dest_path)


# Symlink root-level dependencies
for package in lock_file_details:
    symlink_to_root(package["name"])
    # symlink package to itself, to avoid edge case of package requiring itself
    # create_symlink(
    #     NODE_MODULES_DIR / package["name"],
    #     NODE_MODULES_DIR / package["name"] / "node_modules" / package["name"],
    # )

    symlink_dependencies(package["name"], package["dependencies"])

print("Packages installed successfully.")


# 7. Run postinstall scripts
for package in lock_file_details:
    package_dir = NODE_MODULES_DIR / package["name"]
    postinstall_script = package_dir / "node_modules" / ".bin" / "postinstall"
    if postinstall_script.exists():
        print(f"Running postinstall script for {package['name']}")
        os.system(f"cd {package_dir} && {postinstall_script}")
print("Postinstall scripts run successfully.")
