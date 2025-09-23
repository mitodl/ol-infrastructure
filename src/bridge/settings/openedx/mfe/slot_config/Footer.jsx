import React, { useContext, useEffect } from "react";
import { Button, Dropdown, Collapsible, Hyperlink, Image } from "@openedx/paragon";
import { ExpandLess, ExpandMore } from '@openedx/paragon/icons';
import { getConfig } from '@edx/frontend-platform';
import { PluginSlot } from '@openedx/frontend-plugin-framework';
import { getLoginRedirectUrl } from '@edx/frontend-platform/auth';
import { AppContext } from '@edx/frontend-platform/react';

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

  useEffect(() => {
    const allowedRedirects = ["mitxonline", "xpro"];
    if (
      config.APP_ID === "learning" &&
      allowedRedirects.some((name) => process.env.DEPLOYMENT_NAME?.includes(name)) &&
      authenticatedUser === null
    ) {
      const destination = getLoginRedirectUrl(window.location.href);
      window.location.replace(destination);
    }
  }, [config, authenticatedUser]);

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
