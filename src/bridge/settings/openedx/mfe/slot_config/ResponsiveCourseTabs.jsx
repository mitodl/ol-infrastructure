import React, { useState, useRef, useLayoutEffect, useEffect, useCallback } from 'react';
import { useSelector } from 'react-redux';
import { Dropdown } from '@openedx/paragon';
import { useIntl, FormattedMessage } from '@edx/frontend-platform/i18n';
import { useModel } from './src/generic/model-store';

const moreMessage = {
    id: 'learn.course.tabs.navigation.overflow.menu',
    description: 'The title of the overflow menu for course tabs',
    defaultMessage: 'More...',
};

// Course tabs with explicit overflow-to-dropdown behavior.
const ResponsiveCourseTabs = ({ activeTabSlug }) => {
  const intl = useIntl();
  const moreLabel = intl.formatMessage(moreMessage);

  const courseId = useSelector(
    state => state.courseHome?.courseId || state.courseware?.courseId
  );
  const courseHomeMeta = useModel('courseHomeMeta', courseId);
  const tabs = courseHomeMeta?.tabs;

  const containerRef = useRef(null);
  const tabWidthsRef = useRef([]);
  const moreWidthRef = useRef(0);
  // -1 = not yet calculated; show all tabs initially so measuring works
  const [splitIndex, setSplitIndex] = useState(-1);

  // Step 1: Measure the natural width of each tab using hidden elements
  useLayoutEffect(() => {
    if (!containerRef.current || !tabs?.length) return;

    const measureEls = containerRef.current.querySelectorAll('[data-measure-tab]');
    const moreEl = containerRef.current.querySelector('[data-measure-more]');

    tabWidthsRef.current = Array.from(measureEls).map(
      el => Math.ceil(el.getBoundingClientRect().width)
    );
    if (moreEl) {
      moreWidthRef.current = Math.ceil(moreEl.getBoundingClientRect().width);
    }
  }, [tabs]);

  // Step 2: Calculate how many tabs fit in available space
  const calculate = useCallback(() => {
    if (!containerRef.current || !tabs?.length || !tabWidthsRef.current.length) {
      return;
    }

    // The parent <nav> from Tabs.jsx is our width constraint
    const nav = containerRef.current.closest('nav');
    if (!nav) return;

    const availableWidth = Math.floor(nav.getBoundingClientRect().width);
    const widths = tabWidthsRef.current;
    const moreWidth = moreWidthRef.current;

    // Check if ALL tabs fit without needing "More..."
    const totalWidth = widths.reduce((sum, w) => sum + w, 0);
    if (totalWidth <= availableWidth) {
      setSplitIndex(tabs.length);
      return;
    }

    // Find how many tabs fit WITH the "More..." button taking up space
    let usedWidth = moreWidth;
    let count = 0;
    for (let i = 0; i < widths.length; i++) {
      if (usedWidth + widths[i] <= availableWidth) {
        usedWidth += widths[i];
        count++;
      } else {
        break;
      }
    }

    // Always show at least 1 tab
    setSplitIndex(Math.max(count, 1));
  }, [tabs]);

  // Step 3: Recalculate on mount and whenever the <nav> resizes
  useEffect(() => {
    calculate();

    const nav = containerRef.current?.closest('nav');
    if (!nav) return undefined;

    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(() => {
        window.requestAnimationFrame(calculate);
      });
      observer.observe(nav);
      return () => observer.disconnect();
    }

    const handleResize = () => window.requestAnimationFrame(calculate);
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, [calculate]);

  if (!tabs?.length) return null;

  const effectiveSplit = splitIndex === -1 ? tabs.length : splitIndex;
  const visibleTabs = tabs.slice(0, effectiveSplit);
  const overflowTabs = tabs.slice(effectiveSplit);

  return (
    <div ref={containerRef} style={{ display: 'contents' }}>
      {/* Visible tab links */}
      {visibleTabs.map(({ url, title, slug }) => (
        <a
          key={slug}
          href={url}
          className={`nav-item flex-shrink-0 nav-link${slug === activeTabSlug ? ' active' : ''}`}
        >
          {title}
        </a>
      ))}

      {/* "More..." dropdown for overflow tabs */}
      {overflowTabs.length > 0 && (
        <div className="pgn__tab_more nav-item flex-shrink-0 nav-link responsive-tabs-overflow">
          <Dropdown className="h-100">
            <Dropdown.Toggle
              variant="link"
              className="nav-link h-100"
              id="responsive-tabs-more-menu"
            >
              <FormattedMessage {...moreMessage} />
            </Dropdown.Toggle>
            <Dropdown.Menu className="responsive-tabs-dropdown-menu">
              {overflowTabs.map(({ url, title, slug }) => (
                <Dropdown.Item
                  key={slug}
                  href={url}
                  className={slug === activeTabSlug ? 'active' : ''}
                >
                  {title}
                </Dropdown.Item>
              ))}
            </Dropdown.Menu>
          </Dropdown>
        </div>
      )}

      {/* Hidden measuring elements — same classes as real tabs for accurate sizing */}
      <div
        aria-hidden="true"
        style={{
          // Keep measurement nodes out of document flow and scroll width.
          position: 'fixed',
          visibility: 'hidden',
          pointerEvents: 'none',
          display: 'flex',
          flexWrap: 'nowrap',
          top: -10000,
          left: -10000,
          zIndex: -1,
        }}
      >
        {tabs.map(({ title, slug }) => (
          <span
            key={`measure-${slug}`}
            data-measure-tab
            className="nav-item flex-shrink-0 nav-link"
            style={{ whiteSpace: 'nowrap' }}
          >
            {title}
          </span>
        ))}
        <span
          data-measure-more
          className="nav-item flex-shrink-0 nav-link"
          style={{ whiteSpace: 'nowrap' }}
        >
          {moreLabel}
        </span>
      </div>
    </div>
  );
};

export default ResponsiveCourseTabs;
