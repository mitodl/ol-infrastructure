from concourse.pipelines.open_edx.mfe.pipeline import MFEAppVars, OpenEdxVars

mitx = [
    OpenEdxVars(
        marketing_site_domain="lms-ci.mitx.mit.edu",
        environment="mitx-ci",
        favicon_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/favicon.ico",
        lms_domain="lms-ci.mitx.mit.edu",
        logo_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/logo.png",
        release_name="open-release/nutmeg.master",
        site_name="MITx Residential CI",
        studio_domain="studio-ci.mitx.mit.edu",
        support_url="odl.zendesk.com/hc/en-us/requests/new",
        terms_of_service_url="https://lms-ci.mitx.mit.edu/tos",
    ),
    OpenEdxVars(
        marketing_site_domain="mitx-qa.mitx.mit.edu",
        environment="mitx-qa",
        favicon_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/favicon.ico",
        lms_domain="mitx-qa.mitx.mit.edu",
        logo_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/logo.png",
        release_name="open-release/nutmeg.master",
        site_name="MITx Residential QA",
        studio_domain="studio-mitx-qa.mitx.mit.edu",
        support_url="odl.zendesk.com/hc/en-us/requests/new",
        terms_of_service_url="https://mitx-qa.mitx.mit.edu/tos",
    ),
    OpenEdxVars(
        marketing_site_domain="lms.mitx.mit.edu",
        environment="mitx-production",
        favicon_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/favicon.ico",
        lms_domain="lms.mitx.mit.edu",
        logo_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/logo.png",
        release_name="open-release/nutmeg.master",
        site_name="MITx Residential",
        studio_domain="studio.mitx.mit.edu",
        support_url="odl.zendesk.com/hc/en-us/requests/new",
        terms_of_service_url="https://lms.mitx.mit.edu/tos",
    ),
]

mitx_staging = [
    OpenEdxVars(
        environment="mitx-staging-ci",
        favicon_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/favicon.ico",
        lms_domain="staging-ci.mitx.mit.edu",
        logo_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/logo.png",
        marketing_site_domain="staging-ci.mitx.mit.edu",
        release_name="open-release/nutmeg.master",
        site_name="MITx Residential Staging CI",
        studio_domain="studio-staging-ci.mitx.mit.edu",
        support_url="odl.zendesk.com/hc/en-us/requests/new",
        terms_of_service_url="https://staging-ci.mitx.mit.edu/tos",
    ),
    OpenEdxVars(
        environment="mitx-staging-qa",
        favicon_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/favicon.ico",
        lms_domain="mitx-qa-draft.mitx.mit.edu",
        logo_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/logo.png",
        marketing_site_domain="mitx-qa-draft.mitx.mit.edu",
        release_name="open-release/nutmeg.master",
        site_name="MITx Residential Staging QA",
        studio_domain="studio-mitx-qa-draft.mitx.mit.edu",
        support_url="odl.zendesk.com/hc/en-us/requests/new",
        terms_of_service_url="https://mitx-qa-draft.mitx.mit.edu/tos",
    ),
    OpenEdxVars(
        environment="mitx-staging-production",
        favicon_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/favicon.ico",
        lms_domain="staging.mitx.mit.edu",
        logo_url="https://raw.githubusercontent.com/mitodl/mitx-theme/master/lms/static/images/logo.png",
        marketing_site_domain="staging.mitx.mit.edu",
        release_name="open-release/nutmeg.master",
        site_name="MITx Residential Staging",
        studio_domain="studio-staging.mitx.mit.edu",
        support_url="odl.zendesk.com/hc/en-us/requests/new",
        terms_of_service_url="https://staging.mitx.mit.edu/tos",
    ),
]

mitxonline = [
    OpenEdxVars(
        contact_url="https://mitxonline.zendesk.com/hc/en-us/requests/new",
        environment="mitxonline-qa",
        favicon_url="https://raw.githubusercontent.com/mitodl/mitxonline-theme/main/lms/static/images/favicon.ico",
        honor_code_url="https://rc.mitxonline.mit.edu/honor-code/",
        lms_domain="courses-qa.mitxonline.mit.edu",
        logo_url="https://raw.githubusercontent.com/mitodl/mitxonline-theme/main/lms/static/images/logo.png",
        marketing_site_domain="rc.mitxonline.mit.edu",
        release_name="master",
        site_name="MITx Online QA",
        studio_domain="studio-qa.mitxonline.mit.edu",
        support_url="mitx-micromasters.zendesk.com/hc/",
        terms_of_service_url="https://rc.mitxonline.mit.edu/terms-of-service/",
        trademark_text="© MITxOnline. All rights reserved except where noted.",
    ),
    OpenEdxVars(
        contact_url="https://mitxonline.zendesk.com/hc/en-us/requests/new",
        environment="mitxonline-production",
        favicon_url="https://raw.githubusercontent.com/mitodl/mitxonline-theme/main/lms/static/images/favicon.ico",
        honor_code_url="https://mitxonline.mit.edu/honor-code/",
        lms_domain="lms-production.mitx.mit.edu",
        logo_url="https://raw.githubusercontent.com/mitodl/mitxonline-theme/main/lms/static/images/logo.png",
        marketing_site_domain="courses.mitxonline.mit.edu",
        release_name="master",
        site_name="MITx Online",
        studio_domain="studio.mitxonline.mit.edu",
        support_url="mitx-micromasters.zendesk.com/hc/",
        terms_of_service_url="https://mitxonline.mit.edu/terms-of-service/",
        trademark_text="© MITxOnline. All rights reserved except where noted.",
    ),
]

xpro = [
    OpenEdxVars(
        environment="xpro-ci",
        favicon_url="https://raw.githubusercontent.com/mitodl/mitxpro-theme/master/lms/static/images/favicon.ico",
        honor_code_url="https://ci.xpro.mit.edu/honor-code/",
        lms_domain="courses-ci.xpro.mit.edu",
        logo_url="https://raw.githubusercontent.com/mitodl/mitxpro-theme/master/lms/static/images/logo.png",
        marketing_site_domain="ci.xpro.mit.edu",
        release_name="open-release/maple.master",
        site_name="MIT xPRO CI",
        studio_domain="studio-ci.xpro.mit.edu",
        support_url="xpro.zendesk.com/hc",
        terms_of_service_url="https://ci.xpro.mit.edu/terms-of-service/",
    ),
    OpenEdxVars(
        environment="xpro-qa",
        favicon_url="https://raw.githubusercontent.com/mitodl/mitxpro-theme/master/lms/static/images/favicon.ico",
        honor_code_url="https://rc.xpro.mit.edu/honor-code/",
        lms_domain="courses-rc.xpro.mit.edu",
        logo_url="https://raw.githubusercontent.com/mitodl/mitxpro-theme/master/lms/static/images/logo.png",
        marketing_site_domain="rc.xpro.mit.edu",
        release_name="open-release/maple.master",
        site_name="MIT xPRO RC",
        studio_domain="studio-rc.xpro.mit.edu",
        support_url="xpro.zendesk.com/hc",
        terms_of_service_url="https://rc.xpro.mit.edu/terms-of-service/",
    ),
    OpenEdxVars(
        environment="xpro-production",
        favicon_url="https://raw.githubusercontent.com/mitodl/mitxpro-theme/master/lms/static/images/favicon.ico",
        honor_code_url="https://xpro.mit.edu/honor-code/",
        lms_domain="courses.xpro.mit.edu",
        logo_url="https://raw.githubusercontent.com/mitodl/mitxpro-theme/master/lms/static/images/logo.png",
        marketing_site_domain="xpro.mit.edu",
        release_name="open-release/maple.master",
        site_name="MIT xPRO",
        studio_domain="studio.xpro.mit.edu",
        support_url="xpro.zendesk.com/hc",
        terms_of_service_url="https://xpro.mit.edu/terms-of-service/",
    ),
]

gradebook = MFEAppVars(
    path="gradebook",
    repository="https://github.com/edx/frontend-app-gradebook.git",
    node_major_version=16,
)

courseware = MFEAppVars(
    path="learn",
    repository="https://github.com/edx/frontend-app-learning.git",
    node_major_version=16,
)

library_authoring = MFEAppVars(
    path="authoring",
    repository="https://github.com/edx/frontend-app-library_authoring.git",
    node_major_version=16,
)


deployments: dict[str, list[OpenEdxVars]] = {
    "mitx": mitx,
    "mitx-staging": mitx_staging,
    "mitxonline": mitxonline,
    "xpro": xpro,
}

apps: dict[str, MFEAppVars] = {
    "authoring": library_authoring,
    "gradebook": gradebook,
    "learn": courseware,
}
