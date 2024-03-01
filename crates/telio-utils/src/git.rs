/// Get the commit sha that was used when building.
/// If not present defaults to "dev".
pub fn commit_sha() -> &'static str {
    option_env!("LIBTELIO_COMMIT_SHA").unwrap_or("dev")
}

/// Get the version placeholder (half of the maximum length of git tag)
/// Will be replaced during build promotions
pub fn version_tag() -> &'static str {
    "VERSION_PLACEHOLDER@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@"
}
