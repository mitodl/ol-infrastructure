import json

import pulumi_fastly as fastly
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import iam, route53, s3

from bridge.lib.constants import FASTLY_A_TLS_1_2, FASTLY_CNAME_TLS_1_3
from bridge.lib.magic_numbers import FIVE_MINUTES
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

ocw_site_config = Config("ocw_site")
stack_info = parse_stack()
aws_config = AWSBase(
    tags={
        "OU": "open-courseware",
        "Environment": f"applications_{stack_info.env_suffix}",
    }
)

dns_stack = StackReference("infrastructure.aws.dns")
ocw_zone = dns_stack.require_output("ocw")
# Create S3 buckets
# There are two buckets for each environment (QA, Production):
# One for the that environment's draft site (where authors test content
# changes), and one for the environment's live site.
# See http://docs.odl.mit.edu/ocw-next/s3-buckets

draft_bucket_name = f"ocw-content-draft-{stack_info.env_suffix}"
draft_bucket_arn = f"arn:aws:s3:::{draft_bucket_name}"
live_bucket_name = f"ocw-content-live-{stack_info.env_suffix}"
live_bucket_arn = f"arn:aws:s3:::{live_bucket_name}"

draft_backup_bucket_name = f"ocw-content-backup-draft-{stack_info.env_suffix}"
draft_backup_bucket_arn = f"arn:aws:s3:::{draft_backup_bucket_name}"
live_backup_bucket_name = f"ocw-content-backup-live-{stack_info.env_suffix}"
live_backup_bucket_arn = f"arn:aws:s3:::{live_backup_bucket_name}"

draft_bucket = s3.Bucket(
    draft_bucket_name,
    bucket=draft_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{draft_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)
draft_backup_bucket = s3.Bucket(
    draft_backup_bucket_name,
    bucket=draft_backup_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{draft_backup_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
    versioning=s3.BucketVersioningArgs(enabled=True),
    lifecycle_rules=[
        s3.BucketLifecycleRuleArgs(
            enabled=True,
            noncurrent_version_expiration=s3.BucketLifecycleRuleNoncurrentVersionExpirationArgs(
                days=90,
            ),
        )
    ],
)

live_bucket = s3.Bucket(
    live_bucket_name,
    bucket=live_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{live_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)
live_backup_bucket = s3.Bucket(
    live_backup_bucket_name,
    bucket=live_backup_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{live_backup_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
    versioning=s3.BucketVersioningArgs(enabled=True),
    lifecycle_rules=[
        s3.BucketLifecycleRuleArgs(
            enabled=True,
            noncurrent_version_expiration=s3.BucketLifecycleRuleNoncurrentVersionExpirationArgs(
                days=90,
            ),
        )
    ],
)

policy_description = (
    "Access controls for the CDN to be able to read from the"
    f"{stack_info.env_suffix} website buckets"
)
s3_bucket_iam_policy = iam.Policy(
    f"ocw-site-{stack_info.env_suffix}-policy",
    description=policy_description,
    path=f"/ol-applications/ocw-site/{stack_info.env_suffix}/",
    name_prefix=f"ocw-site-content-read-only-{stack_info.env_suffix}",
    policy=lint_iam_policy(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:ListBucket*",
                        "s3:GetObject*",
                    ],
                    "Resource": [
                        draft_bucket_arn,
                        f"{draft_bucket_arn}/*",
                        draft_backup_bucket_arn,
                        f"{draft_backup_bucket_arn}/*",
                        live_bucket_arn,
                        f"{live_bucket_arn}/*",
                        live_backup_bucket_arn,
                        f"{live_backup_bucket_arn}/*",
                    ],
                }
            ],
        },
        stringify=True,
    ),
)

# NOTE (TMM 2022-03-28): Once we integrate Fastly into this project code we'll likely
# want to turn this domain object into a dictionary of draft and live domains to be
# templated into the Fastly distribution as well.
for domain in ocw_site_config.get_object("domains") or []:
    # If it's a 3 level domain then it's rooted at MIT.edu which means that we are
    # creating an Apex record in Route53. This means that we have to use an A record. If
    # it's deeper than 3 levels then it's a subdomain of ocw.mit.edu and we can use a
    # CNAME.
    record_type = "A" if len(domain.split(".")) == 3 else "CNAME"
    record_value = (
        [str(addr) for addr in FASTLY_A_TLS_1_2]
        if record_type == "A"
        else [FASTLY_CNAME_TLS_1_3]
    )
    route53.Record(
        f"ocw-site-dns-record-{domain}",
        name=domain,
        type=record_type,
        ttl=FIVE_MINUTES,
        records=record_value,
        zone_id=ocw_zone["id"],
    )

export(
    "ocw_site_buckets",
    {
        "buckets": [
            draft_bucket_name,
            draft_backup_bucket_name,
            live_bucket_name,
            live_backup_bucket_name,
        ],
        "policy": s3_bucket_iam_policy.name,
    },
)

#################
# Fastly Config #
#################
# Website Storage Bucket
website_storage_bucket_fqdn = "ocw-website-storage.s3.us-east-1.amazonaws.com"

for purpose in ["draft", "live"]:
    website_bucket_fqdn = (
        f"ocw-content-{purpose}-{stack_info.env_suffix}.s3.us-east-1.amazonaws.com"
    )

    servicevcl_backend = fastly.ServiceVcl(
        f"ocw-{purpose}-{stack_info.env_suffix}",
        backends=[
            fastly.ServiceVclBackendArgs(
                address=website_bucket_fqdn,
                name="WebsiteBucket",
                override_host=website_bucket_fqdn,
                port=443,
                request_condition="not course media or old akamai",
                ssl_cert_hostname=website_bucket_fqdn,
                # ssl_client_cert=pulumi.secret(""),
                # ssl_client_key=pulumi.secret(""),
                ssl_sni_hostname=website_bucket_fqdn,
                use_ssl=True,
            ),
            fastly.ServiceVclBackendArgs(
                address=website_storage_bucket_fqdn,
                name="OCWWebsiteStorageBucket",
                override_host=website_storage_bucket_fqdn,
                port=443,
                request_condition="is old Akamai file",
                ssl_cert_hostname=website_storage_bucket_fqdn,
                # ssl_client_cert=pulumi.secret(""),
                # ssl_client_key=pulumi.secret(""),
                ssl_sni_hostname=website_storage_bucket_fqdn,
                use_ssl=True,
            ),
        ],
        comment="",
        conditions=[
            fastly.ServiceVclConditionArgs(
                name="not course media or old akamai",
                statement='req.url.path !~ "^/coursemedia" && req.url.path !~ "^/ans\\d+"',
                type="REQUEST",
            ),
            fastly.ServiceVclConditionArgs(
                name="Generated by synthetic response for robots.txt",
                priority=0,
                statement='req.url.path == "/robots.txt"',
                type="REQUEST",
            ),
            fastly.ServiceVclConditionArgs(
                name="Generated by synthetic response for 503 page",
                priority=0,
                statement="beresp.status == 503",
                type="CACHE",
            ),
            fastly.ServiceVclConditionArgs(
                name="Generated by synthetic response for 404 page",
                statement="beresp.status == 404",
                type="CACHE",
            ),
            fastly.ServiceVclConditionArgs(
                name="is old Akamai file",
                statement='req.url.path ~ "^/ans\\d+" && req.url.path !~ "/ans7870/21f/21f.027"',
                type="REQUEST",
            ),
        ],
        default_ttl=86400,
        dictionaries=[
            fastly.ServiceVclDictionaryArgs(
                name="redirects",
            )
        ],
        domains=[
            fastly.ServiceVclDomainArgs(name=domain)
            for domain in ocw_site_config.get_object("domains")
        ],
        gzips=[
            fastly.ServiceVclGzipArgs(
                content_types=[
                    "text/html",
                    "application/x-javascript",
                    "text/css",
                    "application/javascript",
                    "text/javascript",
                    "application/json",
                    "application/vnd.ms-fontobject",
                    "application/x-font-opentype",
                    "application/x-font-truetype",
                    "application/x-font-ttf",
                    "application/xml",
                    "font/eot",
                    "font/opentype",
                    "font/otf",
                    "image/svg+xml",
                    "image/vnd.microsoft.icon",
                    "text/plain",
                    "text/xml",
                ],
                extensions=[
                    "css",
                    "js",
                    "html",
                    "eot",
                    "ico",
                    "otf",
                    "ttf",
                    "json",
                    "svg",
                ],
                name="Generated by default gzip policy",
            )
        ],
        headers=[
            fastly.ServiceVclHeaderArgs(
                action="set",
                destination="http.Surrogate-Key",
                name="S3 Cache Surrogate Keys",
                priority=10,
                source="beresp.http.x-amz-meta-site-id",
                type="cache",
            ),
            fastly.ServiceVclHeaderArgs(
                action="set",
                destination="http.Strict-Transport-Security",
                name="Generated by force TLS and enable HSTS",
                source='"max-age=300"',
                type="response",
            ),
        ],
        name="OCW Draft QA",
        request_settings=[
            fastly.ServiceVclRequestSettingArgs(
                force_ssl=True,
                name="Generated by force TLS and enable HSTS",
                xff="",
            )
        ],
        response_objects=[
            fastly.ServiceVclResponseObjectArgs(
                cache_condition="Generated by synthetic response for 404 page",
                content="""<!doctype html>
    <html lang="en">
      <head>


    <link href="/static/css/www.93a2f.css" rel="stylesheet">
    <link
    href="//cdn-images.mailchimp.com/embedcode/classic-10_7.css"
    rel="stylesheet"
    type="text/css"
    />



        <script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':
        new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],
        j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src=
        'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);
        })(window,document,'script','dataLayer','GTM-NMQZ25T');</script>


      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">

      <meta name="description" content="MIT OpenCourseWare is a web-based publication of virtually all MIT course content. OCW is open and available to the world and is a permanent MIT activity">
      <meta name="keywords" content="opencourseware,MIT OCW,courseware,MIT opencourseware,Free Courses,class notes,class syllabus,class materials,tutorials,online courses,MIT courses">

      <script type="application/ld+json">
          {
            "@context": "http://schema.org/",
            "@type" : "WebPage",
            "name": "MIT OpenCourseWare",
            "description": "MIT OpenCourseWare is a web-based publication of virtually all MIT course content. OCW is open and available to the world and is a permanent MIT activity",
            "license": "http://creativecommons.org/licenses/by-nc-sa/4.0/",
            "publisher": {
              "@type": "CollegeOrUniversity",
              "name": "MIT OpenCourseWare"
            }
          }
      </script>

      <title>Page Not Found | MIT OpenCourseWare | Free Online Course Materials</title>


      <link href="/static/css/main.93a2f.css" rel="stylesheet">
      <link href="https://fonts.googleapis.com/css?family=Roboto:400,500&display=swap" rel="stylesheet">
      <style>
      @font-face {
        font-family: 'Material Icons';
        font-style: normal;
        font-weight: 400;
        src: local('Material Icons'),
          local('MaterialIcons-Regular'),
          url(/static/fonts/MaterialIcons-Regular.woff2) format('woff2'),
          url(/static/fonts/MaterialIcons-Regular.woff) format('woff'),
          url(/static/fonts/MaterialIcons-Regular.ttf) format('truetype');
      }
      @font-face {
        font-family: 'Material Icons Round';
        font-style: normal;
        font-weight: 400;
        src: local("MaterialIcons-Round"),
        url(/static/fonts/MaterialIconsRound-Regular.woff2) format('woff2');
      }
      @font-face {
        font-family: 'Cardo Bold';
        font-style: normal;
        font-weight: bold;
        src: local('Cardo'),
          local('Cardo-Bold'),
          url(/static/fonts/Cardo-Bold.ttf) format('truetype');
      }
      @font-face {
        font-family: 'Cardo Italic';
        font-style: italic;
        font-weight: 400;
        src: local('Cardo'),
          local('Cardo-Italic'),
          url(/static/fonts/Cardo-Italic.ttf) format('truetype');
      }
      @font-face {
        font-family: 'Helvetica Light';
        font-style: normal`;
        font-weight: 400;
        src: local('Helvetica Light'),
          local('Helvetica-Light'),
          url(/static/fonts/Helvetica-Light.ttf) format('truetype');
      }
      </style>
    </head>

      <body>


          <noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-NMQZ25T"
          height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>


        <div>
          <div id="notification_dfd74bfc-ec71-4b4e-b226-a185bd364905" class="notification d-none">
      <div class="notification-message">
        <p><strong>Welcome to NextGen OCW!  Help us improve this site.</strong> <a href="/pages/welcome-to-the-nextgen-ocw-beta-site"><strong>Learn more</strong></a><strong>.</strong></p>

      </div>
      <div class="notification-close">
        <i class="material-icons">close</i>
      </div>
    </div>

    <div class="page-single">


    <div
      id="mobile-header"
      class="position-relative bg-black medium-and-below-only"
    >
      <nav class="navbar navbar-expand-lg navbar-dark bg-black">
        <button
          class="navbar-toggler"
          type="button"
          data-toggle="collapse"
          data-target="#navbarSupportedContent"
          aria-controls="navbarSupportedContent"
          aria-expanded="false"
          aria-label="Toggle navigation"
        >
          <i class="material-icons display-4 text-white align-bottom">menu</i>
        </button>
        <div class="mx-auto">
          <a href="/">
            <img
              width="250"
              src="/images/ocw_logo_white.png"
              alt="MIT OpenCourseWare"
            />
          </a>
        </div>
        <div class="collapse navbar-collapse" id="navbarSupportedContent">
          <ul class="navbar-nav mr-auto pl-2 pt-2">
            <li class="nav-item">


        <a class="nav-link search-icon pr-6" href="/search">
          <i class="material-icons">search</i>
        </a>

    </li>
            <li class="nav-item">
              <a
                class="nav-link"
                href="https://giving.mit.edu/give/to/ocw/?utm_source=ocw&utm_medium=homepage_banner&utm_campaign=nextgen_home"
                >Give Now</a
              >
            </li>
            <li class="nav-item">
              <a class="nav-link" href="/about">About OCW</a>
            </li>
            <li class="nav-item">
              <a class="nav-link" href="https://mitocw.zendesk.com/hc/en-us" target="_blank">Help & Faqs</a>
            </li>
            <li class="nav-item">
              <a class="nav-link" href="/contact">
                Contact Us
              </a>
            </li>
          </ul>
        </div>
      </nav>
    </div>


    <div id="desktop-header">
      <div class="contents px-6">
        <div class="left">
          <div class="ocw-logo mr-6">
            <a href="/">
              <img src="/images/ocw_logo_white.png" alt="MIT OpenCourseWare" />
            </a>
          </div>
        </div>
        <div class="right">



        <a class="nav-link search-icon pr-6" href="/search">
          <i class="material-icons">search</i>
        </a>


          <a
            class="d-flex give-button-header py-2 px-3"
            href="https://giving.mit.edu/give/to/ocw/?utm_source=ocw&utm_medium=homepage_banner&utm_campaign=nextgen_home"
          >
            <span class="font-weight-bold w-100">Give now</span>
          </a>
          <a href="/about">
            <div class="text-white font-weight-bold w-100 px-3 pl-5 py-2">
              About OCW
            </div>
          </a>
          <a href="https://mitocw.zendesk.com/hc/en-us" target="_blank">
            <div class="text-white font-weight-bold w-100 px-3 py-2">
              help & faqs
            </div>
          </a>
          <a href="/contact">
            <div class="text-white font-weight-bold w-100 px-3 py-2">
              contact us
            </div>
          </a>
        </div>
      </div>
    </div>

      <div class="page-title">
      <div class="title-text m-auto h1 m-0">
      Page Not Found
      </div>
    </div>

      <div class="container standard-width mx-auto mt-5">
        <h3 id="sorry-the-page-you-requested-was-not-found">Sorry, the page you requested was not found. </h3>
    <p> </p>
    <p>You might want to try <a href="https://ocwnext-rc.odl.mit.edu/search/">searching</a> for another page, or you can <a href="https://ocwnext-rc.odl.mit.edu/contact/">contact us</a> and let us know.</p>

      </div>
       <footer id="footer-container">
      <div class="row pb-4 mx-0 justify-content-between">
        <div>
          <a
            id="open-learning-logo"
            href="https://openlearning.mit.edu/"
            target="_blank"
          >
            <img src="/images/mit-ol.png" alt="MIT Open Learning" />
          </a>
        </div>
        <div
          class="d-flex md-and-above-only align-items-center support-link-container"
        >
          <a href="https://accessibility.mit.edu">Accessibility</a>
          <a href="https://creativecommons.org/licenses/by-nc-sa/4.0/"
            >Creative Commons License</a
          >
          <a href="/pages/privacy-and-terms-of-use/">Terms and Conditions</a>
        </div>
      </div>
      <div class="row pb-4 mx-0 justify-content-between row-gap-20">
        <div class="about-courseware">
          <p>
            MIT OpenCourseWare is an online publication of materials from over 2,500
            MIT courses, freely sharing knowledge with learners and educators around
            the world. <a href="/about">Learn more</a>
          </p>
        </div>
        <div
          class="d-flex sm-and-below-only align-items-start support-link-column flex-column row-gap-20"
        >
          <a href="https://accessibility.mit.edu">Accessibility</a>
          <a href="https://creativecommons.org/licenses/by-nc-sa/4.0/"
            >Creative Commons License</a
          >
          <a href="/pages/privacy-and-terms-of-use/">Terms and Conditions</a>
        </div>
        <div class="row mx-0 align-items-center">
          <p class="font-weight-bold">
            PROUD MEMBER OF :
            <a href="https://www.oeglobal.org/" target="_blank"
              ><img class="oeg-logo" src="/images/oeg_logo.png" alt="Open Education Global"
            /></a>
          </p>
        </div>
      </div>
      <div class="row mx-0 justify-content-between flex-wrap-reverse row-gap-20">
        <div class="d-flex align-items-end mr-3">
          <p>
            © 2001–2022 Massachusetts Institute of Technology
          </p>
        </div>
        <div class="horizontal-list">
          <ul class="p-0">
            <li>
              <a
                class="img-link"
                href="https://www.facebook.com/MITOCW"
                target="_blank"
              >
                <img
                  class="footer-social-icon"
                  src="/images/Facebook.png"
                  alt="facebook"
                />
              </a>
            </li>
            <li>
              <a
                class="img-link"
                href="https://www.instagram.com/mitocw"
                target="_blank"
              >
                <img
                  class="footer-social-icon"
                  src="/images/Instagram.png"
                  alt="instagram"
                />
              </a>
            </li>
            <li>
              <a class="img-link" href="https://twitter.com/MITOCW" target="_blank">
                <img class="footer-social-icon" src="/images/Twitter.png" alt="twitter" />
              </a>
            </li>
            <li>
              <a
                class="img-link"
                href="https://www.youtube.com/mitocw"
                target="_blank"
              >
                <img class="footer-social-icon" src="/images/Youtube.png" alt="youtube" />
              </a>
            </li>
            <li>
              <a
                class="img-link"
                href="https://www.linkedin.com/company/mit-opencourseware/"
                target="_blank"
              >
                <img
                  class="footer-social-icon"
                  src="/images/LinkedIn.png"
                  alt="LinkedIn"
                />
              </a>
            </li>
          </ul>
        </div>
      </div>
    </footer>

    </div>

        </div>

        <script src="/static/js/www.93a2f.js"></script>
        <script src="/static/js/main.93a2f.js"></script>


        <script async src="https://w.appzi.io/w.js?token=Tgs1d"></script>


      </body>
    </html>
    """,
                content_type="text/html",
                name="Generated by synthetic response for 404 page",
                response="Not Found",
                status=404,
            ),
            fastly.ServiceVclResponseObjectArgs(
                cache_condition="Generated by synthetic response for 503 page",
                content="""<!DOCTYPE html>
    <html>
      <head>
        <meta charset="UTF-8">
        <title>503</title>
      </head>
      <body>
        503
      </body>
    </html>""",
                content_type="text/html",
                name="Generated by synthetic response for 503 page",
                response="Service Unavailable",
                status=503,
            ),
            fastly.ServiceVclResponseObjectArgs(
                content="""User-Agent: *
    Disallow: /""",
                content_type="text/plain",
                name="Generated by synthetic response for robots.txt",
                request_condition="Generated by synthetic response for robots.txt",
            ),
        ],
        snippets=[
            fastly.ServiceVclSnippetArgs(
                content=r""" if (beresp.status == 404) {
      set beresp.ttl = 5m;
      set beresp.http.Cache-Control = "max-age=300";
      return(deliver);
    }

    if (bereq.url.path ~ "^/main\.[0-9a-f]+\.(css|js)") {
        set beresp.ttl = 2629743s;  // one month
        set beresp.http.Cache-Control = "max-age=2629743";
    }""",
                name="TTLs setup",
                priority=110,
                type="fetch",
            ),
            fastly.ServiceVclSnippetArgs(
                content="""table departments BOOL {
      "chemistry": true,
      "biological-engineering": true,
      "aeronautics-and-astronautics": true,
      "global-studies-and-languages": true,
      "mathematics": true,
      "health-sciences-and-technology": true,
      "edgerton-center": true,
      "womens-and-gender-studies": true,
      "urban-studies-and-planning": true,
      "sloan-school-of-management": true,
      "anthropology": true,
      "engineering-systems-division": true,
      "chemical-engineering": true,
      "earth-atmospheric-and-planetary-sciences": true,
      "music-and-theater-arts": true,
      "athletics-physical-education-and-recreation": true,
      "architecture": true,
      "experimental-study-group": true,
      "literature": true,
      "mechanical-engineering": true,
      "electrical-engineering-and-computer-science": true,
      "materials-science-and-engineering": true,
      "history": true,
      "linguistics-and-philosophy": true,
      "institute-for-data-systems-and-society": true,
      "biology": true,
      "civil-and-environmental-engineering": true,
      "comparative-media-studies-writing": true,
      "nuclear-engineering": true,
      "economics": true,
      "concourse": true,
      "brain-and-cognitive-sciences": true,
      "science-technology-and-society": true,
      "political-science": true,
      "media-arts-and-sciences": true,
      "physics": true,
      "supplemental-resources": true
    }""",
                name="Departments Table",
                type="init",
            ),
            fastly.ServiceVclSnippetArgs(
                content="""set bereq.url = querystring.remove(bereq.url);
    set bereq.url = regsub(bereq.url, "/$", "/index.html");
    set bereq.url = regsub(bereq.url, "^/ans7870", "/largefiles");
    set bereq.url = regsub(bereq.url, "^/ans15436", "/zipfiles");
    """,
                name="S3 Bucket Proxying",
                priority=200,
                type="miss",
            ),
            fastly.ServiceVclSnippetArgs(
                content=r"""declare local var.location STRING;
    declare local var.status INTEGER;
    declare local var.last_path_part STRING;

    # Perform redirects with dictionary lookups
    declare local var.redirect STRING;
    declare local var.url STRING;

    set var.url = regsub(req.url.path, "/$", "");
    set var.redirect = table.lookup(redirects, var.url);

    if (var.redirect ~ "^\s*?([0-9]{3})\|([^|]*)\|(.*)$") {

      set var.status = std.atoi(re.group.1);

      if (std.strlen(req.url.qs) > 0) {
        set var.location = re.group.3 + if(re.group.2 == "keep", "?" + req.url.qs, "");
      } else {
        set var.location = re.group.3;
      }
      set var.location = regsub(var.location, "{{AK_HOSTHEADER}}", req.http.host);
      set var.location = regsub(var.location, "{{PMUSER_PATH}}", "");


      error 307 var.location;
    }

    # Redirect bare directory names to add a trailing slash
    if (req.url.path !~ "^/static" && req.url.path ~ "/([^/]+)$") {               // ends with non-slash character
      set var.last_path_part = re.group.1;
      if (var.last_path_part !~ "\.[a-zA-Z0-9]+$") {  // is directory, not file w. extension
        set req.http.slash_header = req.url.path + "/";
        if (std.strlen(req.url.qs) > 0) {
          set req.http.slash_header = req.http.slash_header + "?" + req.url.qs;
        }
        error 301 req.http.slash_header;
      }
    }

    # OCW Legacy Department Redirects
    if (req.url.path ~ "(/courses/)([\w-]+)/(.*)/") {
      if (table.lookup_bool(departments, re.group.2, false)) {
        if (std.strlen(re.group.4) > 0) {
          set req.http.header_sans_department = re.group.1 + re.group.3 + "/" + re.group.4;
        } else {
          set req.http.header_sans_department = re.group.1 + re.group.3 + "/";
        }
        error 601 "redirect";
      }
    }

    # OCW Legacy /resources/ to /courses/ redirect
    if (req.url.path ~ "(^/resources/)") {
      set req.http.courses_instead_resources = regsub(req.url.path, "/resources/", "/courses/");
      error 604 "redirect";
    }

    # OCW Legacy remove index.htm
    if (req.url.path ~ "/index.htm$") {
      set req.http.remove_index_htm = regsub(req.url.path, "/index.htm", "/");
      error 605 "redirect";
    }""",
                name="Redirects",
                type="recv",
            ),
            fastly.ServiceVclSnippetArgs(
                content=r"""# Add /pages/ to URL in case of 404
    if (beresp.status == 404) {
      if (req.url.path ~ "(/courses/)([\w-]+)/([\w-]+)/(.*)" && !req.http.redirected) {
        if (std.strlen(re.group.4) > 0) {
          set req.http.pages_header = re.group.1 + re.group.2 + "/resources/" + re.group.4;
        } else {
          set req.http.pages_header = re.group.1 + re.group.2 + "/pages/" + re.group.3;
        }
        error 602 "redirect";
      }
      error 902 "Fastly Internal"; # Let the synthetic 404 take over
    }""",
                name="Legacy OCW Pages Redirect",
                type="fetch",
            ),
            fastly.ServiceVclSnippetArgs(
                content="""if (req.url.ext == "css") {
      set beresp.http.Content-type = "text/css";
    }

    if (req.url.ext == "js") {
      set beresp.http.Content-type = "application/javascript";
    }

    if (req.url.ext == "png") {
      set beresp.http.Content-type = "image/png";
    }

    if (req.url.ext == "jpg") {
      set beresp.http.Content-type = "image/jpeg";
    }

    if (req.url.ext == "gif") {
      set beresp.http.Content-type = "image/gif";
    }

    if (req.url.ext == "pdf") {
      set beresp.http.Content-type = "application/pdf";
    }
    """,
                name="Set correct Content-type for S3 assets",
                type="fetch",
            ),
            fastly.ServiceVclSnippetArgs(
                content="""# Remove AWS headers returned from S3
    unset resp.http.x-amz-id-2;
    unset resp.http.x-amz-request-id;
    unset resp.http.x-amz-version-id;
    unset resp.http.x-amz-meta-s3cmd-attrs;
    unset resp.http.server;

    # Remove unnecessary headers that add weight
    unset resp.http.via;
    unset resp.http.x-timer;

    # Handle repeat 404
    declare local var.same_url STRING;
    set var.same_url = "https://" req.http.host req.url;

    if(var.same_url == resp.http.location) {
      set resp.status = 404;
      return(restart);
    }""",
                name="Clean response headers and handle 404 on delivery",
                type="deliver",
            ),
            fastly.ServiceVclSnippetArgs(
                content="""if (obj.status == 601 && obj.response == "redirect") {
      set obj.status = 302;
      set obj.http.Location = req.protocol + "://" + req.http.host + req.http.header_sans_department;
      return (deliver);
    }

    if (obj.status == 602 && obj.response == "redirect" && !req.http.redirected) {
      set obj.status = 302;
      set obj.http.Location = req.protocol + "://" + req.http.host + req.http.pages_header;
      set req.http.redirected = "1";
      return (deliver);
    }

    if (obj.status == 604 && obj.response == "redirect") {
      set obj.status = 302;
      set obj.http.location = req.http.courses_instead_resources;
      return(deliver);
    }

    if (obj.status == 605 && obj.response == "redirect") {
      set obj.status = 302;
      set obj.http.location = req.http.remove_index_htm;
      return(deliver);
    }

    if (obj.status == 301) {
      set obj.status = 302;
      set obj.http.Location = req.protocol + "://" + req.http.host + req.http.slash_header;
      return (deliver);
    }

    if (obj.status == 307) {
      set obj.http.Location = obj.response;
      return(deliver);
    }""",
                name="Reroute Redirects",
                type="error",
            ),
        ],
        stale_if_error=True,
        opts=ResourceOptions(protect=True),
    )

    items = fastly.ServiceDictionaryItems(
        f"ocw-{purpose}-{stack_info.env_suffix}",
        service_id=servicevcl_backend.id,
        dictionary_id=servicevcl_backend.dictionaries.id,
        items={
            "/1-00F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/civil-and-environmental-engineering/1-00-introduction-to-computers-and-engineering-problem-solving-spring-2012/",
            "/1-050F04": "307|keep|https://{{AK_HOSTHEADER}}/courses/civil-and-environmental-engineering/1-051-structural-engineering-design-fall-2003/",
            "/1-061F08": "307|keep|https://{{AK_HOSTHEADER}}/courses/civil-and-environmental-engineering/1-061-transport-processes-in-the-environment-fall-2008/",
            "/1-258JS17": "307|discard|https://{{AK_HOSTHEADER}}/courses/civil-and-environmental-engineering/1-258j-public-transportation-systems-spring-2017/",
            "/1-72F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/civil-and-environmental-engineering/1-72-groundwater-hydrology-fall-2005/",
            "/10-34F15": "307|discard|https://{{AK_HOSTHEADER}}/courses/chemical-engineering/10-34-numerical-methods-applied-to-chemical-engineering-fall-2015/",
            "/11-124F11": "307|keep|https://{{AK_HOSTHEADER}}/courses/urban-studies-and-planning/11-124-introduction-to-education-looking-forward-and-looking-back-on-education-fall-2011/",
            "/11-309S06": "307|keep|https://{{AK_HOSTHEADER}}/courses/urban-studies-and-planning/11-309j-sensing-place-photography-as-inquiry-fall-2012/",
            "/11-382S21": "307|discard|https://{{AK_HOSTHEADER}}/courses/urban-studies-and-planning/11-382-water-diplomacy-spring-2021/",
            "/11-401F15": "307|discard|https://{{AK_HOSTHEADER}}/courses/urban-studies-and-planning/11-401-introduction-to-housing-community-and-economic-development-fall-2015/",
            "/11-601F16": "307|discard|https://{{AK_HOSTHEADER}}/courses/urban-studies-and-planning/11-601-introduction-to-environmental-policy-and-planning-fall-2016/",
            "/11-941F03": "307|keep|https://{{AK_HOSTHEADER}}/courses/urban-studies-and-planning/11-941-use-of-joint-fact-finding-in-science-intensive-policy-disputes-part-i-fall-2003/",
            "/11-945F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/urban-studies-and-planning/11-945-springfield-studio-fall-2005/",
            "/11-949S04": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-877j-computational-evolutionary-biology-fall-2005/",
            "/11-954S05": "307|keep|https://{{AK_HOSTHEADER}}/courses/urban-studies-and-planning/11-949-city-visions-past-and-future-spring-2004/",
            "/11-965IAP07": "307|keep|https://{{AK_HOSTHEADER}}/courses/urban-studies-and-planning/11-965-reflective-practice-an-approach-for-expanding-your-learning-frontiers-january-iap-2007/",
            "/11-969Su2005": "307|keep|https://{{AK_HOSTHEADER}}/courses/urban-studies-and-planning/11-969-workshop-on-deliberative-democracy-and-dispute-resolution-summer-2005/",
            "/12-000F03": "307|keep|https://{{AK_HOSTHEADER}}/courses/earth-atmospheric-and-planetary-sciences/12-000-solving-complex-problems-fall-2003/",
            "/12-003f08": "307|keep|https://{{AK_HOSTHEADER}}/courses/earth-atmospheric-and-planetary-sciences/12-003-atmosphere-ocean-and-climate-dynamics-fall-2008/",
            "/14-01F18": "307|keep|https://{{AK_HOSTHEADER}}/courses/economics/14-01-principles-of-microeconomics-fall-2018/",
            "/14-01SCF10": "307|keep|https://{{AK_HOSTHEADER}}/courses/economics/14-01sc-principles-of-microeconomics-fall-2011/",
            "/14-13S20": "307|discard|https://{{AK_HOSTHEADER}}/courses/economics/14-13-psychology-and-economics-spring-2020/",
            "/14-73S11": "307|keep|https://{{AK_HOSTHEADER}}/courses/economics/14-73-the-challenge-of-world-poverty-spring-2011/",
            "/14-772S13": "307|keep|https://{{AK_HOSTHEADER}}/courses/economics/14-772-development-economics-macroeconomics-spring-2013/",
            "/15-031JS12": "307|keep|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-031j-energy-decisions-markets-and-policies-spring-2012/",
            "/15-071S17": "307|discard|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-071-the-analytics-edge-spring-2017/",
            "/15-356S12": "307|keep|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-356-how-to-develop-breakthrough-products-and-services-spring-2012/",
            "/15-390F13": "307|keep|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-390-new-enterprises-spring-2013/",
            "/15-401F08": "307|keep|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-401-finance-theory-i-fall-2008/",
            "/15-871F13": "307|keep|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-871-introduction-to-system-dynamics-fall-2013/",
            "/15-879S14": "307|keep|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-879-research-seminar-in-system-dynamics-spring-2014/",
            "/15-960F17": "307|discard|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-960-new-executive-thinking-social-impact-technology-projects-fall-2017-spring-2018/",
            "/15-S08S20": "307|discard|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-s08-fintech-shaping-the-financial-world-spring-2020/",
            "/15-S12F18": "307|keep|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-s12-blockchain-and-money-fall-2018/",
            "/15-S21IAP14": "307|discard|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-s21-nuts-and-bolts-of-business-plans-january-iap-2014/",
            "/15-S50IAP15": "307|keep|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-s50-poker-theory-and-analytics-january-iap-2015/",
            "/15-S50IAP16": "307|discard|https://{{AK_HOSTHEADER}}/courses/sloan-school-of-management/15-s50-how-to-win-at-texas-holdem-poker-january-iap-2016/",
            "/16-01F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/aeronautics-and-astronautics/16-01-unified-engineering-i-ii-iii-iv-fall-2005-spring-2006/",
            "/16-06F12": "307|keep|https://{{AK_HOSTHEADER}}/courses/aeronautics-and-astronautics/16-06-principles-of-automatic-control-fall-2012/",
            "/16-346F08": "307|keep|https://{{AK_HOSTHEADER}}/courses/aeronautics-and-astronautics/16-346-astrodynamics-fall-2008/",
            "/16-412JS16": "307|discard|https://{{AK_HOSTHEADER}}/courses/aeronautics-and-astronautics/16-412j-cognitive-robotics-spring-2016/",
            "/16-660IAP08": "307|keep|https://{{AK_HOSTHEADER}}/courses/aeronautics-and-astronautics/16-660j-introduction-to-lean-six-sigma-methods-january-iap-2012/",
            "/16-660JIAP12": "307|keep|https://{{AK_HOSTHEADER}}/courses/aeronautics-and-astronautics/16-660j-introduction-to-lean-six-sigma-methods-january-iap-2012/",
            "/16-687IAP19": "307|keep|https://{{AK_HOSTHEADER}}/courses/aeronautics-and-astronautics/16-687-private-pilot-ground-school-january-iap-2019/",
            "/16-810IAP07": "307|keep|https://{{AK_HOSTHEADER}}/courses/aeronautics-and-astronautics/16-810-engineering-design-and-rapid-prototyping-january-iap-2007/",
            "/16-842F15": "307|discard|https://{{AK_HOSTHEADER}}/courses/aeronautics-and-astronautics/16-842-fundamentals-of-systems-engineering-fall-2015/",
            "/16-885F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/aeronautics-and-astronautics/16-885j-aircraft-systems-engineering-fall-2005/",
            "/16-90S14": "307|discard|https://{{AK_HOSTHEADER}}/courses/aeronautics-and-astronautics/16-90-computational-methods-in-aerospace-engineering-spring-2014/",
            "/18-01F06": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-01-single-variable-calculus-fall-2006/",
            "/18-01SCF10": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-01sc-single-variable-calculus-fall-2010/",
            "/18-01sc": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-01sc-single-variable-calculus-fall-2010/",
            "/18-02F07": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-02-multivariable-calculus-fall-2007/",
            "/18-02SCF10": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-02sc-multivariable-calculus-fall-2010/",
            "/18-02sc": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-02sc-multivariable-calculus-fall-2010/",
            "/18-031S18": "307|discard|https://{{AK_HOSTHEADER}}/courses/mathematics/18-031-system-functions-and-the-laplace-transform-spring-2018/",
            "/18-03S06": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-03-differential-equations-spring-2010/",
            "/18-03SCF11": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-03sc-differential-equations-fall-2011/",
            "/18-05S14": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-05-introduction-to-probability-and-statistics-spring-2014/",
            "/18-065S18": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-065-matrix-methods-in-data-analysis-signal-processing-and-machine-learning-spring-2018/",
            "/18-06S05": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-06-linear-algebra-spring-2010/",
            "/18-06SCF11": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-06sc-linear-algebra-fall-2011/",
            "/18-085F07": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-085-computational-science-and-engineering-i-fall-2008/",
            "/18-085F08": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-085-computational-science-and-engineering-i-fall-2008/",
            "/18-086S06": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-086-mathematical-methods-for-engineers-ii-spring-2006/",
            "/18-217F19": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-217-graph-theory-and-additive-combinatorics-fall-2019/",
            "/18-404JF20": "307|discard|https://{{AK_HOSTHEADER}}/courses/mathematics/18-404j-theory-of-computation-fall-2020/",
            "/18-650F16": "307|discard|https://{{AK_HOSTHEADER}}/courses/mathematics/18-650-statistics-for-applications-fall-2016/",
            "/18-821S13": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-821-project-laboratory-in-mathematics-spring-2013/",
            "/18-S096F13": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-s096-topics-in-mathematics-with-applications-in-finance-fall-2013/",
            "/18-S906F13": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-s096-topics-in-mathematics-with-applications-in-finance-fall-2013/",
            "/18-S997F11": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-s997-introduction-to-matlab-programming-fall-2011/",
            "/18065videos": "307|discard|https://www.youtube.com/playlist?list=PLUl4u3cNGP63oMNUHXqIUcrkS2PivhN3k",
            "/18065videoslec1": "307|keep|https://www.youtube.com/watch?v=YiqIkSHSmyc&list=PLUl4u3cNGP63oMNUHXqIUcrkS2PivhN3k&index=3",
            "/1806scvideos": "307|keep|https://www.youtube.com/playlist?list=PL221E2BBF13BECF6C",
            "/1806videos": "307|keep|https://www.youtube.com/playlist?list=PLE7DDD91010BC51F8",
            "/1806videoslec7": "307|keep|https://www.youtube.com/watch?v=VqP2tREMvt0&list=PLE7DDD91010BC51F8&index=8",
            "/2-003JF07": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-003j-dynamics-and-control-i-fall-2007/",
            "/2-003SCF11": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-003sc-engineering-dynamics-fall-2011/",
            "/2-00BS08": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-00b-toy-product-design-spring-2008/",
            "/2-086S12": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-086-numerical-computation-for-mechanical-engineers-spring-2013/",
            "/2-087F14": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-087-engineering-math-differential-equations-and-linear-algebra-fall-2014/",
            "/2-29S03": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-29-numerical-marine-hydrodynamics-13-024-spring-2003/",
            "/2-57S12": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-57-nano-to-macro-transport-processes-spring-2012/",
            "/2-60S04": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-60-fundamentals-of-advanced-energy-conversion-spring-2004/",
            "/2-627F11": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-627-fundamentals-of-photovoltaics-fall-2013/",
            "/2-71F04": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-71-optics-spring-2009/",
            "/2-71S09": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-71-optics-spring-2009/",
            "/2-830JS08": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-830j-control-of-manufacturing-processes-sma-6303-spring-2008/",
            "/2-993IAP07": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-993-special-topics-in-mechanical-engineering-the-art-and-science-of-boat-design-january-iap-2007/index.htm",
            "/20-010JS06": "307|keep|https://{{AK_HOSTHEADER}}/courses/biological-engineering/20-010j-introduction-to-bioengineering-be-010j-spring-2006/",
            "/20-020S9": "307|keep|https://{{AK_HOSTHEADER}}/courses/biological-engineering/20-020-introduction-to-biological-engineering-design-spring-2009/",
            "/20-219IAP15": "307|discard|https://{{AK_HOSTHEADER}}/courses/biological-engineering/20-219-becoming-the-next-bill-nye-writing-and-hosting-the-educational-show-january-iap-2015/",
            "/20-309F06": "307|keep|https://{{AK_HOSTHEADER}}/courses/biological-engineering/20-309-biological-engineering-ii-instrumentation-and-measurement-fall-2006/",
            "/2020-vision": "307|keep|https://{{AK_HOSTHEADER}}/resources/res-18-010-a-2020-vision-of-linear-algebra-spring-2020/",
            "/21A-453S04": "307|keep|https://{{AK_HOSTHEADER}}/courses/anthropology/21a-453-anthropology-of-the-middle-east-spring-2004/",
            "/21A.550JS11": "307|keep|https://{{AK_HOSTHEADER}}/courses/anthropology/21a-550j-dv-lab-documenting-science-through-video-and-new-media-fall-2012/",
            "/21F-223F04": "307|keep|https://{{AK_HOSTHEADER}}/courses/global-studies-and-languages/21g-223-listening-speaking-and-pronunciation-fall-2004/",
            "/21G-027F16": "307|discard|https://{{AK_HOSTHEADER}}/courses/global-studies-and-languages/21g-027-asia-in-the-modern-world-images-representations-fall-2016/",
            "/21G-101F14": "307|discard|https://{{AK_HOSTHEADER}}/courses/global-studies-and-languages/21g-101-chinese-i-regular-fall-2014/",
            "/21G-107F14": "307|discard|https://{{AK_HOSTHEADER}}/courses/global-studies-and-languages/21g-107-chinese-i-streamlined-fall-2014/",
            "/21G-503F16": "307|discard|https://{{AK_HOSTHEADER}}/courses/global-studies-and-languages/21g-503-japanese-iii-fall-2016/",
            "/21H-931S04": "307|keep|https://{{AK_HOSTHEADER}}/courses/history/21h-931-seminar-in-historical-methods-spring-2004/",
            "/21L-011F07": "307|keep|https://{{AK_HOSTHEADER}}/courses/literature/21l-011-the-film-experience-fall-2013/",
            "/21L-011F13": "307|keep|https://{{AK_HOSTHEADER}}/courses/literature/21l-011-the-film-experience-fall-2013/",
            "/21L-432S03": "307|keep|https://{{AK_HOSTHEADER}}/courses/literature/21l-432-understanding-television-spring-2003/",
            "/21L-432S08": "307|keep|https://{{AK_HOSTHEADER}}/courses/literature/21l-432-understanding-television-spring-2003/",
            "/21L-448JF10": "307|keep|https://{{AK_HOSTHEADER}}/courses/literature/21l-448j-darwin-and-design-fall-2010/",
            "/21L-705S14": "307|keep|https://{{AK_HOSTHEADER}}/courses/literature/21l-705-major-authors-old-english-and-beowulf-spring-2014/",
            "/21M-235F14": "307|discard|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-235-monteverdi-to-mozart-1600-1800-fall-2013/",
            "/21M-250S14": "307|keep|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-250-beethoven-to-mahler-spring-2014/",
            "/21M-303S09": "307|keep|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-303-writing-in-tonal-forms-i-spring-2009/",
            "/21M-304S09": "307|keep|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-304-writing-in-tonal-forms-ii-spring-2009/",
            "/21M-308S11": "307|keep|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-380-music-and-technology-live-electronics-performance-practices-spring-2011/",
            "/21M-355S13": "307|keep|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-355-musical-improvisation-spring-2013/",
            "/21M-380S11": "307|keep|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-380-music-and-technology-live-electronics-performance-practices-spring-2011/",
            "/21m-220F10": "307|keep|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-220-early-music-fall-2010/",
            "/21m-342f08": "307|keep|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-342-composing-for-jazz-orchestra-fall-2008/",
            "/21m-380f09": "307|keep|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-380-music-and-technology-contemporary-history-and-aesthetics-fall-2009/",
            "/21m-380s10": "307|keep|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-380-music-and-technology-algorithmic-and-generative-music-spring-2010/",
            "/21m-542IAP10": "307|keep|https://{{AK_HOSTHEADER}}/courses/music-and-theater-arts/21m-542-interdisciplinary-approaches-to-musical-time-january-iap-2010/",
            "/22-01F16": "307|keep|https://{{AK_HOSTHEADER}}/courses/nuclear-engineering/22-01-introduction-to-nuclear-engineering-and-ionizing-radiation-fall-2016/",
            "/22-033F11": "307|keep|https://{{AK_HOSTHEADER}}/courses/nuclear-engineering/22-033-nuclear-systems-design-project-fall-2011/",
            "/22-091S08": "307|keep|https://{{AK_HOSTHEADER}}/courses/nuclear-engineering/22-091-nuclear-reactor-safety-spring-2008/",
            "/22-15F14": "307|keep|https://{{AK_HOSTHEADER}}/courses/nuclear-engineering/22-15-essential-numerical-methods-fall-2014/",
            "/24-08JS09": "307|keep|https://{{AK_HOSTHEADER}}/courses/linguistics-and-philosophy/24-08j-philosophical-issues-in-brain-science-spring-2009/",
            "/24-120S09": "307|keep|https://{{AK_HOSTHEADER}}/courses/linguistics-and-philosophy/24-120-moral-psychology-spring-2009/",
            "/24-209S04": "307|keep|https://{{AK_HOSTHEADER}}/courses/linguistics-and-philosophy/24-209-philosophy-in-film-and-other-media-spring-2004/",
            "/24-213F04": "307|keep|https://{{AK_HOSTHEADER}}/courses/linguistics-and-philosophy/24-213-philosophy-of-film-fall-2004/",
            "/24-261F04": "307|keep|https://{{AK_HOSTHEADER}}/courses/linguistics-and-philosophy/24-261-philosophy-of-love-in-the-western-world-fall-2004/",
            "/24-262S04": "307|keep|https://{{AK_HOSTHEADER}}/courses/linguistics-and-philosophy/24-262-feeling-and-imagination-in-art-science-and-technology-spring-2004/",
            "/24-264F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/linguistics-and-philosophy/24-264-film-as-visual-and-literary-mythmaking-fall-2005/",
            "/24-729F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/linguistics-and-philosophy/24-729-topics-in-philosophy-of-language-vagueness-fall-2005/",
            "/24-912S17": "307|discard|https://{{AK_HOSTHEADER}}/courses/linguistics-and-philosophy/24-912-black-matters-introduction-to-black-studies-spring-2017/",
            "/3-021JS12": "307|keep|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-021j-introduction-to-modeling-and-simulation-spring-2012/",
            "/3-054S14": "307|discard|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-054-cellular-solids-structure-properties-and-applications-spring-2015/",
            "/3-054S15": "307|discard|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-054-cellular-solids-structure-properties-and-applications-spring-2015/",
            "/3-091F04": "307|keep|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-091sc-introduction-to-solid-state-chemistry-fall-2010/",
            "/3-091F18": "307|discard|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-091-introduction-to-solid-state-chemistry-fall-2018/",
            "/3-091SCF10": "307|keep|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-091sc-introduction-to-solid-state-chemistry-fall-2010/",
            "/3-091sc": "307|keep|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-091sc-introduction-to-solid-state-chemistry-fall-2010/",
            "/3-185F03": "307|keep|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-185-transport-phenomena-in-materials-engineering-fall-2003/",
            "/3-20F03": "307|keep|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-20-materials-at-equilibrium-sma-5111-fall-2003/",
            "/3-320S05": "307|keep|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-320-atomistic-computer-modeling-of-materials-sma-5107-spring-2005/",
            "/3-53S01": "307|keep|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-53-electrochemical-processing-of-materials-spring-2001/",
            "/3-60F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-60-symmetry-structure-and-tensor-properties-of-materials-fall-2005/",
            "/4-105F12": "307|keep|https://{{AK_HOSTHEADER}}/courses/architecture/4-105-geometric-disciplines-and-architecture-skills-reciprocal-methodologies-fall-2012/",
            "/4-112F12": "307|keep|https://{{AK_HOSTHEADER}}/courses/architecture/4-112-architecture-design-fundamentals-i-nano-machines-fall-2012/",
            "/4-125F02": "307|keep|https://{{AK_HOSTHEADER}}/courses/architecture/4-125-architecture-studio-building-in-landscapes-fall-2002/",
            "/4-196S04": "307|keep|https://{{AK_HOSTHEADER}}/courses/architecture/4-196-architecture-design-level-ii-cuba-studio-spring-2004/",
            "/4-241JS13": "307|keep|https://{{AK_HOSTHEADER}}/courses/architecture/4-241j-theory-of-city-form-spring-2013/",
            "/4-367S06": "307|keep|https://{{AK_HOSTHEADER}}/courses/architecture/4-367-studio-seminar-in-public-art-spring-2006/",
            "/4-370F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/architecture/4-370-interrogative-design-workshop-fall-2005/",
            "/4-421JS13": "307|keep|https://{{AK_HOSTHEADER}}/courses/architecture/4-241j-theory-of-city-form-spring-2013/",
            "/4-614F02": "307|keep|https://{{AK_HOSTHEADER}}/courses/architecture/4-614-religious-architecture-and-islamic-cultures-fall-2002/",
            "/4-696S08": "307|keep|https://{{AK_HOSTHEADER}}/courses/architecture/4-696-a-global-history-of-architecture-writing-seminar-spring-2008/",
            "/5-07SCF13": "307|discard|https://{{AK_HOSTHEADER}}/courses/chemistry/5-07sc-biological-chemistry-i-fall-2013/",
            "/5-08JS16": "307|discard|https://{{AK_HOSTHEADER}}/courses/chemistry/5-08j-biological-chemistry-ii-spring-2016/",
            "/5-111F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/chemistry/5-111-principles-of-chemical-science-fall-2008/",
            "/5-111F08": "307|keep|https://{{AK_HOSTHEADER}}/courses/chemistry/5-111-principles-of-chemical-science-fall-2008/",
            "/5-111F14": "307|discard|https://{{AK_HOSTHEADER}}/courses/chemistry/5-111sc-principles-of-chemical-science-fall-2014/",
            "/5-112F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/chemistry/5-112-principles-of-chemical-science-fall-2005/",
            "/5-307IAP04": "307|keep|https://{{AK_HOSTHEADER}}/courses/chemistry/5-307-chemistry-laboratory-techniques-january-iap-2012/",
            "/5-310F19": "307|discard|https://{{AK_HOSTHEADER}}/courses/chemistry/5-310-laboratory-chemistry-fall-2019/",
            "/5-31F19": "307|discard|https://{{AK_HOSTHEADER}}/courses/chemistry/5-310-laboratory-chemistry-fall-2019/",
            "/5-60S08": "307|keep|https://{{AK_HOSTHEADER}}/courses/chemistry/5-60-thermodynamics-kinetics-spring-2008/",
            "/5-61F17": "307|discard|https://{{AK_HOSTHEADER}}/courses/chemistry/5-61-physical-chemistry-fall-2017/",
            "/5-74S04": "307|keep|https://{{AK_HOSTHEADER}}/courses/chemistry/5-74-introductory-quantum-mechanics-ii-spring-2004/",
            "/5-80F08": "307|keep|https://{{AK_HOSTHEADER}}/courses/chemistry/5-80-small-molecule-spectroscopy-and-dynamics-fall-2008/",
            "/5-95JF15": "307|discard|https://{{AK_HOSTHEADER}}/courses/chemistry/5-95j-teaching-college-level-science-and-engineering-fall-2015/",
            "/5-95JS09": "307|keep|https://{{AK_HOSTHEADER}}/courses/chemistry/5-95j-teaching-college-level-science-and-engineering-spring-2009/",
            "/6-0001F16": "307|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-0001-introduction-to-computer-science-and-programming-in-python-fall-2016/",
            "/6-0002F16": "307|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-0002-introduction-to-computational-thinking-and-data-science-fall-2016/",
            "/6-001S05": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-001-structure-and-interpretation-of-computer-programs-spring-2005/",
            "/6-002S07": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-002-circuits-and-electronics-spring-2007/",
            "/6-003F11": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-003-signals-and-systems-fall-2011/",
            "/6-004S17": "307|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-004-computation-structures-spring-2017/",
            "/6-006F11": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-006-introduction-to-algorithms-fall-2011/",
            "/6-006S20": "307|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-006-introduction-to-algorithms-spring-2020/",
            "/6-007S11": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-007-electromagnetic-energy-from-motors-to-lasers-spring-2011/",
            "/6-00F08": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-00-introduction-to-computer-science-and-programming-fall-2008/",
            "/6-00SCS11": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-00sc-introduction-to-computer-science-and-programming-spring-2011/",
            "/6-013F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-013-electromagnetics-and-applications-fall-2005/",
            "/6-01SCS11": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-01sc-introduction-to-electrical-engineering-and-computer-science-i-spring-2011/",
            "/6-02F12": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-02-introduction-to-eecs-ii-digital-communication-systems-fall-2012/",
            "/6-033S05": "307|keep|https://dspace.mit.edu/handle/1721.1/118791",
            "/6-033S18": "307|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-033-computer-system-engineering-spring-2018/",
            "/6-034F10": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-034-artificial-intelligence-fall-2010/",
            "/6-035F05": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-035-computer-language-engineering-sma-5502-fall-2005/",
            "/6-041F10": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-041-probabilistic-systems-analysis-and-applied-probability-fall-2010/",
            "/6-041SCF13": "307|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-041sc-probabilistic-systems-analysis-and-applied-probability-fall-2013/",
            "/6-042JF10": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-042j-mathematics-for-computer-science-fall-2010/",
            "/6-042JS15": "307|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-042j-mathematics-for-computer-science-spring-2015/",
            "/6-046JF05": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-046j-introduction-to-algorithms-sma-5503-fall-2005/",
            "/6-046JS15": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-046j-design-and-analysis-of-algorithms-spring-2015/",
            "/6-050js08": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-050j-information-and-entropy-spring-2008/",
            "/6-055JS08": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-055j-the-art-of-approximation-in-science-and-engineering-spring-2008/",
            "/6-172F18": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-172-performance-engineering-of-software-systems-fall-2018/",
            "/6-189IAP07": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-189-multicore-programming-primer-january-iap-2007/",
            "/6-262S11": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-262-discrete-stochastic-processes-spring-2011/",
            "/6-370IAP13": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-370-the-battlecode-programming-competition-january-iap-2013/",
            "/6-450F06": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-450-principles-of-digital-communications-i-fall-2006/",
            "/6-451S05": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-451-principles-of-digital-communication-ii-spring-2005/",
            "/6-641s09": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-641-electromagnetic-fields-forces-and-motion-spring-2009/",
            "/6-642f08": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-642-continuum-electromechanics-fall-2008/",
            "/6-811F14": "307|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-811-principles-and-practice-of-assistive-technology-fall-2014/",
            "/6-832s09": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-832-underactuated-robotics-spring-2009/",
            "/6-849F12": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-849-geometric-folding-algorithms-linkages-origami-polyhedra-fall-2012/",
            "/6-851S12": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-851-advanced-data-structures-spring-2012/",
            "/6-858F14": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-858-computer-systems-security-fall-2014/",
            "/6-868JF11": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-868j-the-society-of-mind-fall-2011/",
            "/6-868JS07": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-868j-the-society-of-mind-fall-2011/",
            "/6-890F14": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-890-algorithmic-lower-bounds-fun-with-hardness-proofs-fall-2014/",
            "/6-901f05": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-901-inventions-and-patents-fall-2005/",
            "/6-912IAP06": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-912-introduction-to-copyright-law-january-iap-2006/",
            "/6-S079S13": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-s079-nanomaker-spring-2013/",
            "/6-S095IAP18": "307|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-s095-programming-for-the-puzzled-january-iap-2018/",
            "/6-S897S19": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-s897-machine-learning-for-healthcare-spring-2019/",
            "/7-012F04": "307|keep|https://{{AK_HOSTHEADER}}/courses/biology/7-012-introduction-to-biology-fall-2004/",
            "/7-013S11": "307|keep|https://{{AK_HOSTHEADER}}/courses/biology/7-013-introductory-biology-spring-2013/",
            "/7-014S05": "307|keep|https://{{AK_HOSTHEADER}}/courses/biology/7-014-introductory-biology-spring-2005/",
            "/7-016F18": "307|keep|https://{{AK_HOSTHEADER}}/courses/biology/7-016-introductory-biology-fall-2018/",
            "/7-01SCF11": "307|keep|https://{{AK_HOSTHEADER}}/courses/biology/7-01sc-fundamentals-of-biology-fall-2011/",
            "/7-05S20": "307|discard|https://{{AK_HOSTHEADER}}/courses/biology/7-05-general-biochemistry-spring-2020/",
            "/7-341S18": "307|keep|https://{{AK_HOSTHEADER}}/courses/biology/7-341-the-microbiome-and-drug-delivery-cross-species-communication-in-health-and-disease-spring-2018/",
            "/7-91JS14": "307|keep|https://{{AK_HOSTHEADER}}/courses/biology/7-91j-foundations-of-computational-and-systems-biology-spring-2014/",
            "/8-01F16": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-01sc-classical-mechanics-fall-2016/",
            "/8-03SCF16": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-03sc-physics-iii-vibrations-and-waves-fall-2016/",
            "/8-04S13": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-04-quantum-physics-i-spring-2013/",
            "/8-04S16": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-04-quantum-physics-i-spring-2016/",
            "/8-05F13": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-05-quantum-physics-ii-fall-2013/",
            "/8-06S18": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-06-quantum-physics-iii-spring-2018/",
            "/8-13-14S17": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/8-13F16": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/8-20IAP21": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-20-introduction-to-special-relativity-january-iap-2021/",
            "/8-224S03": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-224-exploring-black-holes-general-relativity-astrophysics-spring-2003/",
            "/8-286F13": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-286-the-early-universe-fall-2013/",
            "/8-333F13": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-333-statistical-mechanics-i-statistical-mechanics-of-particles-fall-2013/",
            "/8-334S14": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-334-statistical-mechanics-ii-statistical-physics-of-fields-spring-2014/",
            "/8-421S14": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-421-atomic-and-optical-physics-i-spring-2014/",
            "/8-422S13": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-422-atomic-and-optical-physics-ii-spring-2013/",
            "/8-591JF14": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-591j-systems-biology-fall-2014/",
            "/8-701F20": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-701-introduction-to-nuclear-and-particle-physics-fall-2020/",
            "/8-821F14": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-821-string-theory-and-holographic-duality-fall-2014/",
            "/8-851S13": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-851-effective-field-theory-spring-2013/",
            "/8-962S20": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-962-general-relativity-spring-2020/",
            "/9-00SC-lec01vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/introduction/removed-clips/",
            "/9-00SC-lec03vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/brain-i/removed-clips/",
            "/9-00SC-lec08vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/consciousness/removed-clips/",
            "/9-00SC-lec09vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/learning/removed-clips/",
            "/9-00SC-lec11vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/memory-ii/removed-clips/",
            "/9-00SC-lec12vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/language-1/removed-clips/",
            "/9-00SC-lec15vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/emotion-motivation/removed-clips/",
            "/9-00SC-lec17vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/child-development/removed-clips/",
            "/9-00SC-lec20vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/psychopathology-i/removed-clips/",
            "/9-00SC-lec21vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/psychopathology-ii/removed-clips/",
            "/9-00SC-lec22vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/social-psychology-i/removed-clips/",
            "/9-00SC-lec23vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/social-psychology-ii/removed-clips/",
            "/9-00SCS11": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/",
            "/9-00sc-lec01vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/introduction/removed-clips/",
            "/9-00sc-lec03vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/brain-i/removed-clips/",
            "/9-00sc-lec08vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/consciousness/removed-clips/",
            "/9-00sc-lec09vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/learning/removed-clips/",
            "/9-00sc-lec11vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/memory-ii/removed-clips/",
            "/9-00sc-lec12vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/language-1/removed-clips/",
            "/9-00sc-lec15vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/emotion-motivation/removed-clips/",
            "/9-00sc-lec17vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/child-development/removed-clips/",
            "/9-00sc-lec20vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/psychopathology-i/removed-clips/",
            "/9-00sc-lec21vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/psychopathology-ii/removed-clips/",
            "/9-00sc-lec22vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/social-psychology-i/removed-clips/",
            "/9-00sc-lec23vid": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-00sc-introduction-to-psychology-fall-2011/social-psychology-ii/removed-clips/",
            "/9-04F13": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-04-sensory-systems-fall-2013/",
            "/9-13S19": "307|discard|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-13-the-human-brain-spring-2019/",
            "/9-14S14": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-14-brain-structure-and-its-origins-spring-2014/",
            "/9-40S18": "307|keep|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-40-introduction-to-neural-computation-spring-2018/",
            "/CMS": "307|keep|https://{{AK_HOSTHEADER}}/courses/comparative-media-studies-writing/",
            "/CMS-608S14": "307|keep|https://{{AK_HOSTHEADER}}/courses/comparative-media-studies-writing/cms-608-game-design-spring-2014/",
            "/CMS-611JF14": "307|keep|https://{{AK_HOSTHEADER}}/courses/comparative-media-studies-writing/cms-611j-creating-video-games-fall-2014/",
            "/CMS-701S15": "307|discard|https://{{AK_HOSTHEADER}}/courses/comparative-media-studies-writing/cms-701-current-debates-in-media-spring-2015/",
            "/CMS-801F04": "307|keep|https://{{AK_HOSTHEADER}}/courses/comparative-media-studies-writing/cms-801-media-in-transition-fall-2012/",
            "/CMS-930F01": "307|keep|https://{{AK_HOSTHEADER}}/courses/comparative-media-studies-writing/cms-930-media-education-and-the-marketplace-fall-2001/",
            "/CMS-S63F19": "307|discard|https://{{AK_HOSTHEADER}}/courses/comparative-media-studies-writing/cms-s63-playful-augmented-reality-audio-design-exploration-fall-2019/",
            "/CMSW": "307|keep|https://{{AK_HOSTHEADER}}/courses/comparative-media-studies-writing/",
            "/ES-S41S12": "307|keep|https://{{AK_HOSTHEADER}}/courses/experimental-study-group/es-s41-speak-italian-with-your-mouth-full-spring-2012/",
            "/ESD-051JF12": "307|keep|https://{{AK_HOSTHEADER}}/courses/engineering-systems-division/esd-051j-engineering-innovation-and-design-fall-2012/",
            "/ESD-172JF09": "307|keep|https://{{AK_HOSTHEADER}}/courses/engineering-systems-division/esd-172j-x-prize-workshop-grand-challenges-in-energy-fall-2009/",
            "/ESD-290S05": "307|keep|https://{{AK_HOSTHEADER}}/courses/engineering-systems-division/esd-290-special-topics-in-supply-chain-management-spring-2005/",
            "/ESD-932S06": "307|keep|https://{{AK_HOSTHEADER}}/courses/engineering-systems-division/esd-932-engineering-ethics-spring-2006/",
            "/ESD-S43S14": "307|keep|https://{{AK_HOSTHEADER}}/courses/engineering-systems-division/esd-s43-green-supply-chain-management-spring-2014/",
            "/HST-725S04": "307|keep|https://{{AK_HOSTHEADER}}/courses/health-sciences-and-technology/hst-725-music-perception-and-cognition-spring-2009/",
            "/HST-931S09": "302|keep|https://{{AK_HOSTHEADER}}/courses/health-sciences-and-technology/hst-921-information-technology-in-the-health-care-system-of-the-future-spring-2009/",
            "/HST-S14S12": "307|keep|https://{{AK_HOSTHEADER}}/courses/health-sciences-and-technology/hst-s14-health-information-systems-to-improve-quality-of-care-in-resource-poor-settings-spring-2012/",
            "/MAS-531F09": "307|keep|https://{{AK_HOSTHEADER}}/courses/media-arts-and-sciences/mas-531-computational-camera-and-photography-fall-2009/",
            "/MAS-771S11": "307|keep|https://{{AK_HOSTHEADER}}/courses/media-arts-and-sciences/mas-771-autism-theory-and-technology-spring-2011/",
            "/MAS-962S10": "307|keep|https://{{AK_HOSTHEADER}}/courses/media-arts-and-sciences/mas-962-special-topics-new-textiles-spring-2010/",
            "/MAS-S62S18": "307|keep|https://{{AK_HOSTHEADER}}/courses/media-arts-and-sciences/mas-s62-cryptocurrency-engineering-and-design-spring-2018/",
            "/SP-235S09": "307|keep|https://{{AK_HOSTHEADER}}/courses/experimental-study-group/es-010-chemistry-of-sports-spring-2013/",
            "/SP-718S09": "307|keep|https://{{AK_HOSTHEADER}}/courses/edgerton-center/ec-710-d-lab-medical-technologies-for-the-developing-world-spring-2010/",
            "/SP-722S05": "307|keep|https://{{AK_HOSTHEADER}}/courses/edgerton-center/ec-s01-internet-technology-in-local-and-global-communities-spring-2005-summer-2005/",
            "/SP-723S07": "307|keep|https://{{AK_HOSTHEADER}}/courses/edgerton-center/ec-715-d-lab-disseminating-innovations-for-the-common-good-spring-2007/",
            "/SP-772S05": "307|keep|https://{{AK_HOSTHEADER}}/courses/edgerton-center/ec-s01-internet-technology-in-local-and-global-communities-spring-2005-summer-2005/",
            "/SP-775S11": "307|keep|https://{{AK_HOSTHEADER}}/courses/edgerton-center/ec-711-d-lab-energy-spring-2011/",
            "/SP-784S09": "307|keep|https://{{AK_HOSTHEADER}}/courses/edgerton-center/ec-721-wheelchair-design-in-developing-countries-spring-2009/",
            "/STS-050S11": "307|keep|https://{{AK_HOSTHEADER}}/courses/science-technology-and-society/sts-050-the-history-of-mit-spring-2011/",
            "/STS-069F02": "307|keep|https://{{AK_HOSTHEADER}}/courses/science-technology-and-society/sts-069-technology-in-a-dangerous-world-fall-2002/",
            "/STS-081JS17": "307|discard|https://{{AK_HOSTHEADER}}/courses/science-technology-and-society/sts-081-innovation-systems-for-science-technology-energy-manufacturing-and-health-spring-2017/",
            "/STS-081S17": "307|keep|https://{{AK_HOSTHEADER}}/courses/science-technology-and-society/sts-081-innovation-systems-for-science-technology-energy-manufacturing-and-health-spring-2017/",
            "/courses/6-002x-related-curriculum": "307|keep|https://{{AK_HOSTHEADER}}/courses/mitx-related-courseware/index.htm",
            "/courses/brain-and-cognitive-sciences/9-70-social-psychology-spring-2009": "307|discard|https://{{AK_HOSTHEADER}}/courses/brain-and-cognitive-sciences/9-70-social-psychology-spring-2013/",
            "/courses/chemical-engineering/10-01-ethics-for-engineers-spring-2020": "307|keep|https://{{AK_HOSTHEADER}}/courses/chemical-engineering/10-01-ethics-for-engineers-artificial-intelligence-spring-2020/",
            "/courses/chemistry/5-72-statistical-mechanics-spring-2012": "307|keep|https://{{AK_HOSTHEADER}}/courses/chemistry/5-72-non-equilibrium-statistical-mechanics-spring-2012/",
            "/courses/civil-and-environmental-engineering/1-203j-logistical-and-transportation-planning-methods-fall-2004": "307|discard|https://{{AK_HOSTHEADER}}/courses/civil-and-environmental-engineering/1-203j-logistical-and-transportation-planning-methods-fall-2006/",
            "/courses/comparative-media-studies-writing/21w-749-documentary-photography-and-photojournalism-still-images-of-a-world-in-motion-spring-2009": "307|discard|https://{{AK_HOSTHEADER}}/courses/comparative-media-studies-writing/21w-749-documentary-photography-and-photojournalism-still-images-of-a-world-in-motion-spring-2016/",
            "/courses/curriculum-map": "307|keep|https://{{AK_HOSTHEADER}}/courses/mit-curriculum-guide/",
            "/courses/earth-atmospheric-and-planetary-sciences/12-090-special-topics-in-earth-atmospheric-and-planetary-sciences-the-environment-of-the-earths-surface-spring-2007": "307|keep|https://{{AK_HOSTHEADER}}/courses/earth-atmospheric-and-planetary-sciences/12-090-the-environment-of-the-earths-surface-spring-2007/",
            "/courses/earth-atmospheric-and-planetary-sciences/12-090-special-topics-in-earth-atmospheric-and-planetary-sciences-the-environment-of-the-earths-surface-spring-2007/exams": "307|discard|https://{{AK_HOSTHEADER}}/courses/earth-atmospheric-and-planetary-sciences/12-090-the-environment-of-the-earths-surface-spring-2007/",
            "/courses/economics/14-01sc-principles-of-microeconomics-fall-2011/Syllabus": "307|keep|https://{{AK_HOSTHEADER}}/courses/economics/14-01sc-principles-of-microeconomics-fall-2011/syllabus/",
            "/courses/editors-picks": "307|keep|https://{{AK_HOSTHEADER}}/courses/most-visited-courses/",
            "/courses/edx-related-courseware": "307|keep|https://{{AK_HOSTHEADER}}/courses/mitx-related-courseware/",
            "/courses/electrical-engineering-and-computer-science/6-003-signals-and-systems-fall-2003": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-003-signals-and-systems-fall-2011/",
            "/courses/electrical-engineering-and-computer-science/6-00sc-introduction-to-computer-science-and-programming-spring-2011/Syllabus": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-00sc-introduction-to-computer-science-and-programming-spring-2011/syllabus/",
            "/courses/electrical-engineering-and-computer-science/6-092-introduction-to-programming-in-java-january-iap-2010/thisShouldNotExist": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-092-introduction-to-programming-in-java-january-iap-2010/",
            "/courses/electrical-engineering-and-computer-science/6-096-introduction-to-c-january-iap-2009": "307|keep|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-096-introduction-to-c-january-iap-2011/",
            "/courses/electrical-engineering-and-computer-science/6-837-computer-graphics-fall-2003": "302|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-837-computer-graphics-fall-2012/",
            "/courses/electrical-engineering-and-computer-science/6-851-advanced-data-structures-spring-2010": "307|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-851-advanced-data-structures-spring-2012/",
            "/courses/electrical-engineering-and-computer-science/6-857-network-and-computer-security-fall-2003": "307|discard|https://{{AK_HOSTHEADER}}/courses/electrical-engineering-and-computer-science/6-857-network-and-computer-security-spring-2014/",
            "/courses/global-studies-and-languages/21f-701-spanish-i-fall-2003": "307|discard|https://{{AK_HOSTHEADER}}/courses/global-studies-and-languages/21g-701-spanish-i-fall-2003/",
            "/courses/linguistics-and-philosophy/24-00-problems-of-philosophy-fall-2005": "307|keep|https://{{AK_HOSTHEADER}}/courses/linguistics-and-philosophy/24-00-problems-in-philosophy-fall-2010/",
            "/courses/materials-science-and-engineering/3-091-introduction-to-solid-state-chemistry-fall-2004": "307|keep|https://{{AK_HOSTHEADER}}/courses/materials-science-and-engineering/3-091sc-introduction-to-solid-state-chemistry-fall-2010/",
            "/courses/mathematics/18-022-calculus-fall-2005": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-022-calculus-of-several-variables-fall-2010/",
            "/courses/mathematics/18-03-differential-equations-spring-2006": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-03-differential-equations-spring-2010/",
            "/courses/mathematics/18-03-differential-equations-spring-2006/index.htm": "307|discard|https://{{AK_HOSTHEADER}}/courses/mathematics/18-03-differential-equations-spring-2010/",
            "/courses/mathematics/18-03sc-differential-equations-fall-2011/Syllabus": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-03sc-differential-equations-fall-2011/syllabus/",
            "/courses/mathematics/18-05-introduction-to-probability-and-statistics-spring-2005": "307|discard|https://{{AK_HOSTHEADER}}/courses/mathematics/18-05-introduction-to-probability-and-statistics-spring-2014/",
            "/courses/mathematics/18-06-linear-algebra-spring-2005": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-06-linear-algebra-spring-2010/index.htm",
            "/courses/mathematics/18-06-linear-algebra-spring-2005/index.htm": "307|discard|https://{{AK_HOSTHEADER}}/courses/mathematics/18-06-linear-algebra-spring-2010/index.htm",
            "/courses/mathematics/18-06sc-linear-algebra-fall-2011/Syllabus": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-06sc-linear-algebra-fall-2011/syllabus/",
            "/courses/mathematics/18-701-algebra-i-fall-2007": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-701-algebra-i-fall-2010/",
            "/courses/mathematics/18-821-project-laboratory-in-mathematics-spring-2013/instructor-insights": "307|discard|https://{{AK_HOSTHEADER}}/courses/mathematics/18-821-project-laboratory-in-mathematics-spring-2013/index.htm",
            "/courses/mathematics/18-915-graduate-topology-seminar-kan-seminar-fall-2014/instructor-insights": "307|discard|https://{{AK_HOSTHEADER}}/courses/mathematics/18-915-graduate-topology-seminar-kan-seminar-fall-2014/index.htm",
            "/courses/mechanical-engineering/2-094-finite-element-analysis-of-solids-and-fluids-spring-2008": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-094-finite-element-analysis-of-solids-and-fluids-ii-spring-2011/",
            "/courses/mechanical-engineering/2-29-numerical-fluid-dynamics-spring-2015": "307|keep|https://{{AK_HOSTHEADER}}/courses/mechanical-engineering/2-29-numerical-fluid-mechanics-spring-2015/",
            "/courses/msthematics/18-02-multivariable-fall-2007/video-lectures/lecture-28-divergence-theorem": "307|keep|https://{{AK_HOSTHEADER}}/courses/mathematics/18-02-multivariable-calculus-fall-2007/video-lectures/lecture-28-divergence-theorem/",
            "/courses/ocean-engineering": "307|keep|https://dspace.mit.edu/handle/1721.1/34000",
            "/courses/physics/8-01-classical-mechanics-fall-2016": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-01sc-classical-mechanics-fall-2016/",
            "/courses/physics/8-04-quantum-physics-i-spring-2013/other": "307|keep|https://{{AK_HOSTHEADER}}/courses/physics/8-04-quantum-physics-i-spring-2013/lecture-videos/",
            "/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2007-spring-2008": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2007-spring-2008/labs/lab10": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2007-spring-2008/labs/lab11": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2007-spring-2008/labs/lab15": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2007-spring-2008/labs/lab16": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2007-spring-2008/labs/lab17": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2007-spring-2008/labs/lab19": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2007-spring-2008/labs/lab20": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2007-spring-2008/labs/lab21": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2007-spring-2008/labs/lab22": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2007-spring-2008/labs/lab6": "307|discard|https://{{AK_HOSTHEADER}}/courses/physics/8-13-14-experimental-physics-i-ii-junior-lab-fall-2016-spring-2017/",
            "/courses/special-programs/sp-272-culture-tech-spring-2003": "307|discard|https://{{AK_HOSTHEADER}}/courses/experimental-study-group/es-272-culture-tech-spring-2003/",
            "/courses/special-programs/sp-287-kitchen-chemistry-spring-2009": "307|discard|https://{{AK_HOSTHEADER}}/courses/experimental-study-group/es-287-kitchen-chemistry-spring-2009/",
            "/courses/subtitled": "307|discard|https://{{AK_HOSTHEADER}}/courses/captioned/",
            "/courses/this-course-at-mit": "307|keep|https://{{AK_HOSTHEADER}}/courses/instructor-insights/",
            "/courses/writing-and-humanistic-studies/21w-755-writing-and-reading-short-stories-spring-2012": "307|keep|https://{{AK_HOSTHEADER}}/courses/comparative-media-studies-writing/21w-755-writing-and-reading-short-stories-spring-2012/",
            "/help/faq-fair-use": "307|keep|https://mitocw.zendesk.com/hc/en-us/sections/4414782875163-FAIR-USE",
            "/help/faq-fair-use/index.htm": "307|keep|https://mitocw.zendesk.com/hc/en-us/sections/4414782875163-FAIR-USE",
            "/terms": "307|keep|https://{{AK_HOSTHEADER}}/pages/privacy-and-terms-of-use/",
            "/terms/index.htm": "307|keep|https://{{AK_HOSTHEADER}}/pages/privacy-and-terms-of-use/",
        },
        opts=ResourceOptions(protect=True),
    )
