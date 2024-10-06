import json
import os
import pickle
import tarfile
from pathlib import Path

import requests

from errors import CacheError, MetadataError, NetworkError
from resolver import resolve_version

# 0. configuration setup
# 1. connection pooling
# 2. get packages list first
# 3. download their metadata
# 3.5 resolve version
# 4. figure out the dependencies
# repeat 3 and 4 for each package
# 5. download the packages
# 6. install the packages
# 7. symlink dependencies of each package to the node_modules directory's package directory


# 0. configuration setup


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


# Setup store and cache
STORE_DIR = Path.cwd() / ".yap_store"  # TODO: update this to home() instead of cwd()
STORE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = STORE_DIR / ".yap_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
NODE_MODULES_DIR = Path.cwd() / "node_modules"
NODE_MODULES_DIR.mkdir(parents=True, exist_ok=True)


def set_to_metadata_cache(name: str, contents: any):
    if "/" in name:
        name = name.replace("/", "_")
    cache_file = CACHE_DIR / name
    try:
        with cache_file.open("wb") as f:
            pickle.dump(contents, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        raise CacheError(f"Error saving to cache for '{name}': {e}")


def get_from_metadata_cache(name: str):
    if "/" in name:
        name = name.replace("/", "_")
    cache_file = CACHE_DIR / name
    if not cache_file.exists():
        return None
    try:
        with cache_file.open("rb") as f:
            return pickle.load(f)
    except Exception as e:
        raise CacheError(f"Error reading from cache for '{name}': {e}")


session = requests.Session()
if "authToken" in CONFIG:
    session.headers["Authorization"] = f"Bearer {CONFIG['authToken']}"


def fetch_package_metadata(name: str) -> dict[str, any]:
    cached_package_metadata = get_from_metadata_cache(name)
    if cached_package_metadata is not None:
        return cached_package_metadata
    package_url = f"{CONFIG["registry"]}{name}"
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


# 2. get packages list first from package.json
package_json = Path.cwd() / "package.json"
if not package_json.exists():
    print("package.json not found")
    exit(1)


dependencies = {}
lock_file_details = []
with open(package_json, "r") as f:
    data = json.load(f)
    dependencies = data.get("dependencies", {})
    dependencies.update(data.get("devDependencies", {}))
    dependencies.update(data.get("peerDependencies", {}))


# 3. download their metadata
def resolve_dependency_and_queue_urls(dependencies):
    for package_name, version in dependencies.items():
        if package_name in METADATA_DOWNLOADED_PACKAGES:
            continue
        print("RESOLVING: ", package_name, version)
        package_metadata = fetch_package_metadata(package_name)
        METADATA_DOWNLOADED_PACKAGES.add(package_name)

        # 3.5 resolve version
        # check if the version starts with something funky like git+ or npm:
        if version.startswith(("git+", "npm:", "git:")):
            continue
        available_versions = list(package_metadata["versions"].keys())
        resolved_version = resolve_version(version, available_versions)
        if resolved_version is None:
            raise MetadataError(
                f"Could not resolve version for {package_name} {version}"
            )
        package_metadata = package_metadata["versions"][resolved_version]

        # 4. figure out the dependencies
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

# 5. download the packages
for package in lock_file_details:
    if package["name"] in TARBALL_DOWNLOADED_PACKAGES:
        continue

    tarball_path = STORE_DIR / f"{package['name'].replace('/', '_')}.tgz"
    if (STORE_DIR / package["name"]).exists():
        print("Package already exists", package["name"])
        continue

    print("Downloading", package["name"])
    response = session.get(package["url"])

    if response.status_code != 200:
        raise NetworkError(
            f"Failed to fetch {package['url']}: {response.status_code} {response.reason}"
        )

    with open(tarball_path, "wb") as f:
        f.write(response.content)

    with tarfile.open(tarball_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.startswith("package/"):
                member.name = member.name[len("package/") :]
                tar.extract(member, path=STORE_DIR / package["name"])
    os.remove(tarball_path)
    TARBALL_DOWNLOADED_PACKAGES.add(package["name"])

# 6. install the packages
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

# 7. symlink dependencies of each package to the node_modules directory's package directory
print("Symlinking dependencies")


def create_symlink(source: Path, destination: Path):
    # Ensure the destination directory exists
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()  # Remove existing link or file if any
    os.symlink(source, destination)


def symlink_dependencies(package_name: str, dependencies: dict):
    # Each dependency is symlinked within the package's node_modules directory
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
    symlink_dependencies(package["name"], package["dependencies"])
