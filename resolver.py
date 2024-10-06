import semver


def resolve_version(package_version: str, available_versions: str):
    if not package_version or package_version == "*":
        return max(available_versions, key=semver.VersionInfo.parse)
    if package_version.startswith(("git+", "npm:", "git:")):
        return None
    constraints = package_version.split()
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
