import React, { useContext, useEffect } from "react";
import { Button, Dropdown, Collapsible, Hyperlink, Image } from "@openedx/paragon";
import { ExpandLess, ExpandMore } from '@openedx/paragon/icons';
import { getConfig } from '@edx/frontend-platform';
import { PluginSlot } from '@openedx/frontend-plugin-framework';
import { getLoginRedirectUrl } from '@edx/frontend-platform/auth';
import { AppContext } from '@edx/frontend-platform/react';
import { useLocation } from 'react-router-dom';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';

function RevealLinks({ label, children }) {

  return (
    <Collapsible.Advanced>
      <div className="d-flex align-items-center">
        <div className="border-top mr-2 flex-grow-1" />
        <Collapsible.Trigger>
          <Button
            data-testid="helpToggleButton"
            variant="outline-primary"
            size="sm"
          >
            <span className="pr-1">{label}</span>
            <Collapsible.Visible whenClosed><ExpandMore /></Collapsible.Visible>
            <Collapsible.Visible whenOpen><ExpandLess /></Collapsible.Visible>
          </Button>
        </Collapsible.Trigger>
        <div className="border-top ml-2 flex-grow-1" />
      </div>
      <Collapsible.Body>
        <div className="d-flex justify-content-center gap-3 align-items-center my-3">
          {children}
        </div>
      </Collapsible.Body>
    </Collapsible.Advanced>
  );
}

function PoweredBy() {
    return (
      <Hyperlink destination="https://openedx.org">
        <Image
          width="120px"
          alt={"Open edX"}
          src="https://logos.openedx.org/open-edx-logo-tag.png"
        />
      </Hyperlink>
    );
  }

export function CopyrightNotice({copyrightText}){
    return (
        <div className="d-flex flex-column justify-content-center mb-3">
        <div className="text-center x-small">{copyrightText}</div>
        <div className="text-center x-small">{"edX and Open edX are registered trademarks of edX LLC."}</div>
        </div>
    );
}

export function MenuLinks({ menuItems }) {
  return (
    <ul className="d-flex flex-column flex-md-row flex-wrap list-unstyled gap-3 gap-md-4 menu-links align-items-center justify-content-center">
      {
        menuItems.map((item) => <li className="mx-2"><Hyperlink destination={ item.url }>{item.title}</Hyperlink></li>)
      }
    </ul>
  );
}


export function Logo({
    imageUrl = 'https://edx-cdn.org/v3/default/logo.svg',
    destinationUrl,
    logoStyle = { maxHeight: '2rem', height: '33px' }
}) {
const image = (
    <Image src={imageUrl} style={logoStyle} />
);

if (destinationUrl === undefined) {
    return image;
}

return (
    <Hyperlink destination={destinationUrl} className="p-0">
    {image}
    </Hyperlink>
);
}

// This is just a dummy config for the default values used when the slot is not overridden.
const config = {
    imageUrl: "dummy image url",
    destinationUrl: "dumy destination url",
    languages: [ // Languages are hidden in our configuration as the functionality is not implemented right now
      { code: "en", name: "English" },
      { code: "es", name: "Español" },
    ],
    centerLinks: [  // Will be used if the slot is not overridden
      {
        label: "First Column",
        links: [
          { label: "About Us", url: "dummy url" },
          { label: "Terms of Service", url: "dummy url" },
          { label: "Privacy Policy", url: "dummy url" },
        ],
      },
      {
        label: "Second Column",
        links: [
            { label: "Honor Code", url: "dummy url" },
            { label: "Accessibility", url: "https://accessibility.mit.edu/" },
        ],
      },
    ],
  };

const ForceLoginRedirect = () => {
  const config = getConfig();
  const { authenticatedUser } = useContext(AppContext);
  const location = useLocation(); // React Router's current page URL
  useEffect(() => {
    const allowedRedirects = ["mitxonline", "xpro"];
    if (
      config.APP_ID === "learning" &&
      allowedRedirects.some((name) => process.env.DEPLOYMENT_NAME?.includes(name)) &&
      authenticatedUser === null
    ) {
      const destination = getLoginRedirectUrl(
        `${process.env.LEARNING_BASE_URL}${location.pathname}${location.search}`
      );
      window.location.replace(destination);
    }
  }, [config, authenticatedUser, location]);

  return null;
};

const AutoSelectLanguage = () => {
  const config = getConfig();
  const location = useLocation();
  const { authenticatedUser } = useContext(AppContext);

  const lmsBaseURL = config.LMS_BASE_URL;
  const studioBaseURL = config.STUDIO_BASE_URL;
  const username = authenticatedUser?.username;
  const DEVELOPMENT_ENVIRONMENT = "development";
  const AUTHORING_APP_ID = "authoring";
  const ENGLISH_LANG_CODE = "en";
  const LANGUAGE_PREFERENCE_COOKIE_NAME =
    config.LANGUAGE_PREFERENCE_COOKIE_NAME ||
    process.env.LANGUAGE_PREFERENCE_COOKIE_NAME ||
    `${process.env.ENVIRONMENT}-openedx-language-preference`;
  const courseKeyRegex = /course-v1:[^/]+/;
  const reloadCookieName = "authoringLangCookieReloaded";

  // Helper to escape regex metacharacters in cookie name
  const escapeRegExp = (string) => string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const getCookie = (name) => {
    const safeName = escapeRegExp(name);
    const match = document.cookie.match(new RegExp(`(^| )${safeName}=([^;]+)`));
    return match ? decodeURIComponent(match[2]) : null;
  };
  const setCookie = (name, value, days = 1, domainAttr = null) => {
    const expires = new Date(Date.now() + days * 864e5).toUTCString();
    document.cookie = `${name}=${encodeURIComponent(value)}; expires=${expires}; path=/${domainAttr};`;
  };
  const removeCookie = (name, domainAttr = null) => {
    document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/${domainAttr};`;
  };

  /**
   * Returns cookie domain attribute string:
   *   "; Domain=example.com"
   * or "" if unable to determine domain.
   *
   * Rules:
   * - If URL contains `lmsBaseURL`, use hostname from `lmsBaseURL` as-is
   * - Otherwise, remove ONLY the first subdomain from the URL hostname
   */
  const getReloadCookieDomainAttr = (url) => {
    try {
      // Case 1: Prefer domain from lmsBaseURL when contained
      if (lmsBaseURL && url.includes(lmsBaseURL)) {
        const lmsHostname = new URL(lmsBaseURL).hostname;
        return lmsHostname ? `; Domain=${lmsHostname}` : "";
      }

      // Case 2: Fallback → remove first subdomain
      const hostname = new URL(url).hostname;
      const parts = hostname.split(".");

      // If hostname has <= 2 labels, nothing to strip
      if (parts.length <= 2) {
        return `; Domain=${hostname}`;
      }

      // Remove ONLY the first subdomain
      const withoutFirstSubdomain = parts.slice(1).join(".");

      return `; Domain=${withoutFirstSubdomain}`;
    } catch (error) {
      if (process.env.NODE_ENV === DEVELOPMENT_ENVIRONMENT) {
        console.error("Failed to parse URL:", error);
      }
      return "";
    }
  };

  async function setLanguage(baseURL, language) {
    try {
      const response = await getAuthenticatedHttpClient().patch(
        `${baseURL}/api/user/v1/preferences/${username}`,
        { "pref-lang": language },
        { headers: { "Content-Type": "application/merge-patch+json" } }
      );
      return response.status === 204;
    } catch (error) {
      if (process.env.NODE_ENV === DEVELOPMENT_ENVIRONMENT) {
        console.error("Language update failed:", error);
      }
      return false;
    }
  }

  // This API is provided by the ol-openedx-course-translations plugin https://github.com/mitodl/open-edx-plugins/tree/main/src/ol_openedx_course_translations
  async function fetchCourseLanguage(courseKey) {
    const url = `${lmsBaseURL}/course-translations/api/course-language/${courseKey}`;
    try {
      const { data } = await getAuthenticatedHttpClient().get(url);
      return data?.language;
    } catch (error) {
      if (process.env.NODE_ENV === DEVELOPMENT_ENVIRONMENT) {
        console.warn("Course language fetch failed:", error);
      }
      return null;
    }
  }

  useEffect(() => {
    if (!username) return;

    const cookieLang = getCookie(LANGUAGE_PREFERENCE_COOKIE_NAME);
    // Studio (Authoring App) logic
    if (process.env.APP_ID === AUTHORING_APP_ID) {
      // Only reset if reload cookie does not exist and language is not English
      if (cookieLang === ENGLISH_LANG_CODE || getCookie(reloadCookieName)) return;

      (async () => {
        try {
          const updated = await setLanguage(studioBaseURL, ENGLISH_LANG_CODE);
          if (updated) {
            const cookieDomainAttr = getReloadCookieDomainAttr(studioBaseURL);
            setCookie(reloadCookieName, "true", 1, cookieDomainAttr);
            window.location.reload();
          }
        } catch (error) {
          if (process.env.NODE_ENV === DEVELOPMENT_ENVIRONMENT) {
            console.error("Failed to set authoring language:", error);
          }
        }
      })();
      return;
    }

    const match = location.pathname.match(courseKeyRegex);
    if (!match) return;

    const courseKey = match[0];
    (async () => {
      const courseLang = await fetchCourseLanguage(courseKey);
      if (
        courseLang &&
        cookieLang &&
        courseLang !== cookieLang
      ) {
        const updated = await setLanguage(lmsBaseURL, courseLang);
        if (updated) {
          const cookieDomainAttr = getReloadCookieDomainAttr(lmsBaseURL);
          removeCookie(reloadCookieName, cookieDomainAttr); // Remove after setting in learning MFE
          window.location.reload();
        }
      }
    })();

  }, [location.pathname, username]);

  return null;
};

const AppziScript = () => {
  const appziUrl = process.env.APPZI_URL;

  useEffect(() => {
    if (!appziUrl) {
      return;
    }
    const script = document.createElement('script');
    script.src = appziUrl;
    script.async = true;
    document.head.appendChild(script);
  }, []);

  return null;
};

const Footer = () => {

  const {
    imageUrl,
    destinationUrl,
    languages = [],
    centerLinks = [],
  } = config;

  return (
    <footer className="d-flex flex-column align-items-stretch">
      <ForceLoginRedirect />
      <AppziScript />
      {
          (process.env.ENABLE_AUTO_LANGUAGE_SELECTION === "true") ? <AutoSelectLanguage /> : null
      }
        <PluginSlot id="frontend.shell.footer.desktop.top.ui">
            <RevealLinks label={"Reveal Button"} />
        </PluginSlot>
        <div className="py-3 px-3 d-flex gap-5 justify-content-between align-items-stretch">
            <div className="flex-basis-0 d-flex align-items-start">
                <div className="d-flex gap-3 align-items-center">
                    <PluginSlot id="frontend.shell.footer.desktop.leftLinks.ui">
                        <div className="d-flex flex-column">
                            <Logo imageUrl={imageUrl} destinationUrl={destinationUrl} />
                        </div>
                    </PluginSlot>
                </div>
            </div>

            <div className="flex-grow-1 flex-basis-0 d-flex justify-content-center">
                <div className="d-flex flex-column justify-content-between gap-5">
                <PluginSlot id="frontend.shell.footer.desktop.centerLinks.ui">
                    <div className="d-flex flex-wrap column-gap-6 row-gap-4" style={{ columnGap: "1.5rem" }}>
                        {centerLinks.map((column, index) => (
                        <div className="d-flex flex-grow-1 flex-column gap-2 small">
                            {column.links.map((link, linkIndex) => (
                                <Hyperlink destination={link.url} key={linkIndex}>
                                    {link.label}
                                </Hyperlink>

                            ))}
                        </div>
                    ))}
                    </div>
                    </PluginSlot>
                    <PluginSlot id="frontend.shell.footer.desktop.legalNotices.ui">
                        <div className="d-flex flex-column justify-content-center mb-3">
                            {/* This footer trademark notice is a legal requirement and cannot be removed or modified. */}
                            <div className="text-center x-small">{"edX and Open edX are registered trademarks of edX LLC."}</div>
                        </div>
                    </PluginSlot>
                </div>
            </div>
            <div className="flex-basis-0 d-flex justify-content-end">
                <div className="d-flex flex-column justify-content-between">
                    <PluginSlot id="frontend.shell.footer.desktop.rightLinks.ui">
                        <div className="d-flex flex-column gap-3 align-items-end flex-grow-1 justify-content-between">
                            <Dropdown>
                                <Dropdown.Toggle variant="outline-primary" size="sm">
                                {languages[0].name}
                                </Dropdown.Toggle>
                                <Dropdown.Menu className="overflow-auto" style={{ maxHeight: '320px' }}>
                                {languages.map((language) => (
                                    <Dropdown.Item key={language.code}>
                                        {language.name}
                                    </Dropdown.Item>
                                ))}
                                </Dropdown.Menu>
                            </Dropdown>

                        </div>
                    </PluginSlot>
                    <PoweredBy />
                </div>
            </div>
        </div>
    </footer>
  );
};

export default Footer;
