import React, { useContext, useEffect, useState, useCallback, useMemo, useRef } from 'react';
import PropTypes from 'prop-types';
import { getConfig } from '@edx/frontend-platform';

import Sidebar from './src/courseware/course/sidebar/Sidebar';
import SidebarContext from './src/courseware/course/sidebar/SidebarContext';
import AIDrawerManagerSidebar from './AIDrawerManagerSidebar';

const AI_DRAWER_MESSAGE_TYPES = [
    'smoot-design::ai-drawer-open',
    'smoot-design::tutor-drawer-open',
];

const SidebarAIDrawerCoordinator = ({ courseId }) => {
    const contextValue = useContext(SidebarContext);
    const currentSidebar = contextValue?.currentSidebar ?? null;
    const toggleSidebar = contextValue?.toggleSidebar ?? (() => { });
    const shouldDisplayFullScreen = contextValue?.shouldDisplayFullScreen ?? false;
    const unitId = contextValue?.unitId ?? null;

    const [showAIDrawer, setShowAIDrawer] = useState(false);
    const prevUnitIdRef = useRef(unitId);
    const showAIDrawerRef = useRef(false);
    const wrapperRef = useRef(null);

    const messageOrigin = useMemo(() => {
        const lmsBaseUrl = getConfig().LMS_BASE_URL;
        if (lmsBaseUrl) {
            try {
                return new URL(lmsBaseUrl).origin;
            } catch (error) {
                // Fallback to window.location.origin
            }
        }
        return window.location.origin;
    }, []);

    const handleAIDrawerMessage = useCallback((event) => {
        if (event.origin !== messageOrigin) {
            return;
        }

        if (event.data?.type && AI_DRAWER_MESSAGE_TYPES.includes(event.data.type)) {
            setShowAIDrawer(true);
            if (currentSidebar !== null) {
                toggleSidebar(null);
            }
        }
    }, [messageOrigin, currentSidebar, toggleSidebar]);

    useEffect(() => {
        window.addEventListener('message', handleAIDrawerMessage);
        return () => {
            window.removeEventListener('message', handleAIDrawerMessage);
        };
    }, [handleAIDrawerMessage]);

    useEffect(() => {
        if (currentSidebar !== null) {
            setShowAIDrawer(false);
        }
    }, [currentSidebar]);

    useEffect(() => {
        showAIDrawerRef.current = showAIDrawer;
    }, [showAIDrawer]);

    useEffect(() => {
        if (prevUnitIdRef.current && prevUnitIdRef.current !== unitId && unitId !== null) {
            // Only send close message if drawer is actually open
            if (showAIDrawerRef.current) {
                window.postMessage(
                    {
                        type: 'smoot-design::ai-drawer-close',
                    },
                    messageOrigin
                );
            }
            setShowAIDrawer(false);
        }
        prevUnitIdRef.current = unitId;
    }, [unitId, messageOrigin]);

    // Keeps --ai-drawer-height in sync with the actual visible area on each
    // scroll/resize so the sticky drawer never overflows the viewport bottom.
    useEffect(() => {
        const wrapper = wrapperRef.current;

        if (!showAIDrawer || shouldDisplayFullScreen) {
            wrapper.style.removeProperty('--ai-drawer-height');
            return undefined;
        }

        const INSET_PX = 16; // matches the `1rem` inset in the CSS rule
        const mq = window.matchMedia('(min-width: 1025px)');

        let rafId = null;
        let resizeObserver = null;

        const update = () => {
            rafId = null;
            if (!mq.matches) {
                wrapper.style.removeProperty('--ai-drawer-height');
                return;
            }
            const parent = wrapper.parentElement;
            if (!parent) return;
            const parentRect = parent.getBoundingClientRect();
            const stickyTop = Math.max(parentRect.top, INSET_PX);
            const effectiveBottom = Math.min(window.innerHeight - INSET_PX, parentRect.bottom);
            const available = effectiveBottom - stickyTop;

            wrapper.style.setProperty(
                '--ai-drawer-height',
                `${Math.max(0, available)}px`,
            );
        };

        const schedule = () => {
            if (rafId == null) {
                rafId = window.requestAnimationFrame(update);
            }
        };

        const attach = () => {
            if (mq.matches) {
                window.addEventListener('scroll', schedule, { passive: true });
                window.addEventListener('resize', schedule);
                if (!resizeObserver && wrapper.parentElement) {
                    resizeObserver = new ResizeObserver(schedule);
                    resizeObserver.observe(wrapper.parentElement);
                }
                schedule();
            } else {
                detach();
            }
        };

        const detach = () => {
            window.removeEventListener('scroll', schedule);
            window.removeEventListener('resize', schedule);
            if (resizeObserver) {
                resizeObserver.disconnect();
                resizeObserver = null;
            }
            if (rafId != null) {
                window.cancelAnimationFrame(rafId);
                rafId = null;
            }
            wrapper.style.removeProperty('--ai-drawer-height');
        };

        mq.addEventListener('change', attach);
        attach();

        return () => {
            mq.removeEventListener('change', attach);
            detach();
        };
    }, [showAIDrawer, shouldDisplayFullScreen]);

    return (
        <>
            {currentSidebar !== null && <Sidebar />}
            <div
                ref={wrapperRef}
                className={`ai-drawer-wrapper ml-0 ml-xl-4 align-top ${shouldDisplayFullScreen ? 'ai-drawer-wrapper-fullscreen' : ''
                } ${showAIDrawer ? '' : 'd-none'}`}
                aria-hidden={!showAIDrawer}
            >
                <AIDrawerManagerSidebar />
            </div>
        </>
    );
};

SidebarAIDrawerCoordinator.propTypes = {
    courseId: PropTypes.string.isRequired,
};

export default SidebarAIDrawerCoordinator;
