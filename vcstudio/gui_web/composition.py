"""Production composition root for the Web and legacy report front ends."""
from __future__ import annotations


def create_api():
    """Create the production API with fail-closed catalysis integrations."""
    from vcstudio.gui_web.api import Api
    from vcstudio.project.catalysis_runtime import (
        create_production_authoring_services,
        create_production_catalysis_services,
    )

    reaction_source, kinetics_provider = create_production_catalysis_services()
    catalysis_authoring, kinetics_authoring = (
        create_production_authoring_services(reaction_source))
    return Api(
        reaction_domain_source=reaction_source,
        kinetics_projection_provider=kinetics_provider,
        catalysis_authoring_service_factory=catalysis_authoring,
        kinetics_authoring_service_factory=kinetics_authoring,
    )


__all__ = ["create_api"]
