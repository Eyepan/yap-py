import requests
from pathlib import Path
import tarfile
import pickle
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import sys
import semver

downloaded_packages = set()
downloaded_packages_lock = threading.Lock()


# Custom exception classes
class PackageNotFoundError(Exception):
    pass


class CacheError(Exception):
    pass


class NetworkError(Exception):
    pass


class MetadataError(Exception):
    pass


# Read config
CONFIG = {"registry": "https://registry.npmjs.org/"}


def process_npmrc(file_path):
    if file_path.exists():
        with open(file_path, "r") as f:
            for line in f:
                if line.startswith("//"):
                    registry, authToken = line.split(":")
                    CONFIG["registry"] = "https:" + registry
                    CONFIG["authToken"] = authToken.split("=")[1].strip()
                else:
                    configName, configValue = line.split("=")
                    CONFIG[configName.strip()] = configValue.strip()


process_npmrc(Path.home() / ".npmrc")
process_npmrc(Path.cwd() / ".npmrc")

if not CONFIG["registry"].endswith("/"):
    CONFIG["registry"] += "/"

# Setup store and cache
STORE_DIR = Path.home() / ".yap_store"
STORE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = STORE_DIR / ".yap_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def set_to_metadata_cache(name: str, contents: any):
    cache_file = CACHE_DIR / name
    try:
        with cache_file.open("wb") as f:
            pickle.dump(contents, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        raise CacheError(f"Error saving to cache for '{name}': {e}")


def get_from_metadata_cache(name: str):
    cache_file = CACHE_DIR / name
    if not cache_file.exists():
        return None
    try:
        with cache_file.open("rb") as f:
            return pickle.load(f)
    except Exception as e:
        raise CacheError(f"Error reading from cache for '{name}': {e}")


session = requests.Session()


def fetch_with_auth(url: str, headers: dict[str, str]) -> requests.Response:
    if "authToken" in CONFIG:
        headers["Authorization"] = f"Bearer {CONFIG['authToken']}"
    response = session.get(url, headers=headers)
    if response.status_code != 200:
        raise NetworkError(
            f"Failed to fetch {url}: {response.status_code} {response.reason}"
        )
    return response


def fetch_json_with_auth(url: str, headers: dict[str, str]) -> dict[str, any]:
    response = fetch_with_auth(url, headers)
    try:
        return response.json()
    except ValueError as e:
        raise MetadataError(f"Error parsing JSON from {url}: {e}")


def fetch_package_metadata(name: str) -> dict[str, any]:
    cached_package_metadata = get_from_metadata_cache(name)
    if cached_package_metadata is not None:
        return cached_package_metadata
    package_url = CONFIG["registry"] + name
    package_headers = {
        "Accept": "application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8, */*"
    }
    package_details = fetch_json_with_auth(package_url, package_headers)
    set_to_metadata_cache(name, package_details)
    return package_details


def extract_tarball_to_store(tarball_url: str, package_name: str):
    package_directory = STORE_DIR / package_name
    if package_directory.exists():
        return

    headers = {}
    if "authToken" in CONFIG:
        headers["Authorization"] = f"Bearer {CONFIG['authToken']}"

    response = session.get(tarball_url, headers=headers, stream=True)
    response.raise_for_status()

    package_directory.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=response.raw, mode="r|gz") as tar:
        tar.extractall(path=package_directory)


def hardlink_package_from_store(package_name: str):
    package_directory = STORE_DIR / package_name
    if not package_directory.exists():
        raise PackageNotFoundError(f"{package_name} not found in the store")
    node_modules_directory = Path.cwd() / "node_modules" / package_name
    if node_modules_directory.exists():
        return
    node_modules_directory.mkdir(parents=True, exist_ok=True)
    for item in package_directory.rglob("*"):
        target = node_modules_directory / item.relative_to(package_directory)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file() and not target.exists():
            target.hardlink_to(item)


def resolve_package_version(given_version: str, available_versions: list[str]) -> str:
    if not given_version or given_version == "*":
        return max(available_versions, key=semver.VersionInfo.parse)
    if given_version.startswith(("git+", "npm:", "git:")):
        return None
    constraints = given_version.split()
    valid_versions = [
        version
        for version in available_versions
        if all(
            check_version(semver.VersionInfo.parse(version), constraint)
            for constraint in constraints
        )
    ]
    return max(valid_versions, key=semver.VersionInfo.parse) if valid_versions else None


def check_version(version_info, constraint):
    if constraint.startswith(">"):
        return version_info > semver.VersionInfo.parse(constraint[1:])
    elif constraint.startswith(">="):
        return version_info >= semver.VersionInfo.parse(constraint[2:])
    elif constraint.startswith("<"):
        return version_info < semver.VersionInfo.parse(constraint[1:])
    elif constraint.startswith("<="):
        return version_info <= semver.VersionInfo.parse(constraint[2:])
    elif constraint.startswith("~"):
        base_version = semver.VersionInfo.parse(constraint[1:])
        return (
            version_info.major == base_version.major
            and version_info.minor == base_version.minor
        )
    elif constraint.startswith("^"):
        base_version = semver.VersionInfo.parse(constraint[1:])
        return (
            version_info.major == base_version.major
            and version_info.minor >= base_version.minor
        ) or (version_info.major > base_version.major)
    elif "-" in constraint:
        lower_bound, upper_bound = map(str.strip, constraint.split("-"))
        return (
            semver.VersionInfo.parse(lower_bound)
            <= version_info
            <= semver.VersionInfo.parse(upper_bound)
        )
    elif "||" in constraint:
        return any(
            check_version(version_info, r.strip()) for r in constraint.split("||")
        )
    else:
        return version_info == semver.VersionInfo.parse(constraint)


def download_package(package_name: str, package_version: str = None):
    with downloaded_packages_lock:
        if package_name in downloaded_packages:
            return

    try:
        package_metadata = fetch_package_metadata(package_name)
        with downloaded_packages_lock:
            downloaded_packages.add(package_name)
        resolved_version = package_metadata.get("dist-tags", {}).get("latest")

        if resolved_version is None and package_version is None:
            raise MetadataError(
                f"No 'latest' version found for package '{package_name}' and no other package_version was provided."
            )

        if package_version is not None:
            available_versions = package_metadata.get("versions", {}).keys()
            resolved_version = resolve_package_version(
                package_version, available_versions=available_versions
            )

        version_metadata = package_metadata.get("versions", {}).get(resolved_version)
        if version_metadata is None:
            print(
                f"No metadata found for version '{resolved_version}' of package '{package_name}'."
            )
            return

        dependencies = version_metadata.get("dependencies", {})
        if dependencies:
            packages_to_download = {
                dep: ver
                for dep, ver in dependencies.items()
                if dep not in downloaded_packages
            }
            download_packages_in_parallel(packages_to_download)

        tarball_link = version_metadata["dist"]["tarball"]
        extract_tarball_to_store(tarball_link, package_name=package_name)

        with downloaded_packages_lock:
            downloaded_packages.add(package_name)

        hardlink_package_from_store(package_name)
    except Exception as e:
        raise MetadataError(f"Failed to download package '{package_name}': {e}")


def download_packages_sequentially(package_details: dict[str, str]):
    for package_name, package_version in package_details.items():
        download_package(package_name, package_version)


progress_tracker = {
    "total_packages": 0,
    "completed_packages": 0,
    "lock": threading.Lock(),
}


def download_packages_in_parallel(package_details: dict[str, str], executor=None):
    if executor is None:
        executor = ThreadPoolExecutor(max_workers=200)
        progress_tracker["total_packages"] += len(package_details)

    def download_and_track(package_name, package_version):
        download_package(package_name, package_version)
        with progress_tracker["lock"]:
            progress_tracker["completed_packages"] += 1
            sys.stdout.write(
                f"🚚[{progress_tracker['completed_packages']}/{progress_tracker['total_packages']}]\r"
            )
            sys.stdout.flush()

    future_to_package = {
        executor.submit(download_and_track, package_name, package_version): package_name
        for package_name, package_version in package_details.items()
    }

    try:
        for future in as_completed(future_to_package):
            package_name = future_to_package[future]
            try:
                future.result()
            except Exception as e:
                print(f"[ERROR] Failed to download {package_name}: {e}")
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted! Cancelling tasks...")
        executor.shutdown(wait=False)
        print("[INFO] All ongoing tasks have been cancelled.")
    finally:
        if executor is not None:
            executor.shutdown()


# Package list
package_list = {
    "npm": None,
    "bun": None,
    "chalk": None,
    "next": None,
    "nuxt": None,
    "typescript": None,
}

# Start downloading packages in parallel
download_packages_in_parallel(package_list)
