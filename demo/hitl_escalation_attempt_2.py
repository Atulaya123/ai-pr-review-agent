def normalize_repo_slug(slug: str) -> str:
    """Lowercases and strips whitespace from a GitHub repo slug."""
    return slug.strip().lower()


def normalize_branch_name(name: str) -> str:
    """Lowercases and strips whitespace from a branch name."""
    return name.strip().lower()


def normalize_label_name(label: str) -> str:
    """Lowercases and strips whitespace from a GitHub label name."""
    return label.strip().lower()
