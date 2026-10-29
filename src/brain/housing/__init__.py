def require_configured() -> None:
    """Reject execution, but not registration, when housing is disabled."""
    from src.orchestrator.config import HOUSING_CORRIDOR

    if not HOUSING_CORRIDOR.configured:
        raise RuntimeError(
            "housing_corridor not configured in user/profile.yaml — "
            "housing search is opt-in. Add a `housing_corridor` block "
            "(anchor / radius_km / cities / postcodes / per-source ids) "
            "to enable."
        )
