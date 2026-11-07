#!/bin/bash
# build-image.sh

get_version() {
    local version_file="src/scietex/adc_service/version.py"
    local version

    # Check if file exists
    if [ ! -f "$version_file" ]; then
        echo "Error: Version file $version_file not found" >&2
        return 1
    fi

    # Extract version by parsing the source directly. Importing the module would
    # execute the package __init__, which pulls in runtime dependencies (msgspec)
    # that are not installed for the system interpreter running this script.
    version=$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$version_file" | head -n1)

    # Validate version is not empty
    if [ -z "$version" ]; then
        echo "Error: Empty version string extracted" >&2
        return 1
    fi

    # Validate version format (semantic versioning pattern)
    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][a-zA-Z0-9]+)?$ ]]; then
        echo "Error: Invalid version format: '$version'" >&2
        return 1
    fi

    echo "$version"
    return 0
}

# Main script execution
main() {
    local version
    local image="registry.buro-nts.ru/scietex-adc-service"
    local temp_manifest="${image}:build-temp"
    local push_latest=false
    local extras="spi"

    # Parse command line arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --latest)
                push_latest=true
                shift
                ;;
            --extras)
                extras="$2"
                shift 2
                ;;
            *)
                echo "Unknown option: $1" >&2
                return 1
                ;;
        esac
    done

    # Get version - declare and assign separately
    if ! version=$(get_version); then
        return 1
    fi

    echo "Building multi-arch image version ${version}..."

    # Build the multi-architecture image. VERSION selects the published wheel to
    # install, so the image matches the PyPI artifact for this version. EXTRAS
    # selects the ADC backend, defaulting to the SPI one.
    if ! podman build --platform linux/amd64,linux/arm64 \
        --build-arg "VERSION=${version}" \
        --build-arg "EXTRAS=${extras}" \
        --manifest "${temp_manifest}" .; then
        echo "Error: Build failed" >&2
        return 1
    fi

    # Push with version tag
    echo "Pushing version ${version}..."
    if ! podman manifest push "${temp_manifest}" "${image}:${version}"; then
        echo "Error: Failed to push version ${version}" >&2
        podman manifest rm "${temp_manifest}" 2>/dev/null
        return 1
    fi

    # Conditionally push as latest
    if [ "$push_latest" = true ]; then
        echo "Pushing as latest..."
        if ! podman manifest push "${temp_manifest}" "${image}:latest"; then
            echo "Error: Failed to push latest tag" >&2
            podman manifest rm "${temp_manifest}" 2>/dev/null
            return 1
        fi
    fi

    # Clean up temporary manifest
    if ! podman manifest rm "${temp_manifest}"; then
        echo "Warning: Failed to remove temporary manifest ${temp_manifest}" >&2
    fi

    echo "Build complete! Images available as:"
    echo "  ${image}:${version}"
    if [ "$push_latest" = true ]; then
        echo "  ${image}:latest"
    fi

    return 0
}

# Run main function and capture exit code
main "$@"
exit_code=$?
exit $exit_code
