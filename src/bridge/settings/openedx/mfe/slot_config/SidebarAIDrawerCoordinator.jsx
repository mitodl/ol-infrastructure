import React, { useContext, useEffect, useState, useCallback, useMemo, useRef } from 'react';
import PropTypes from 'prop-types';
import { getConfig } from '@edx/frontend-platform';

import Sidebar from './src/courseware/course/sidebar/Sidebar';
import SidebarContext from './src/courseware/course/sidebar/SidebarContext';
import AIDrawerManagerSidebar from './AIDrawerManagerSidebar';
import FeedbackDrawerSlot from './FeedbackDrawerSlot';

const AI_DRAWER_MESSAGE_TYPES = [
    'smoot-design::ai-drawer-open',
    'smoot-design::tutor-drawer-open',
];

// When true, the per-block feedback renders INLINE in this sidebar column
// (mutually exclusive with the AskTIM drawer and the discussions sidebar),
// instead of as a right-side overlay. Default off keeps the overlay behavior.
const FEEDBACK_SLOT_MODE = process.env.FEEDBACK_SLOT_MODE === 'true';
const FEEDBACK_OPEN_MESSAGE = 'ol-feedback::drawer-open';
const FEEDBACK_CLOSE_MESSAGE = 'ol-feedback::drawer-close';
const AI_DRAWER_CLOSE_MESSAGE = 'smoot-design::ai-drawer-close';

// Keeps `--ai-drawer-height` synced to the actual visible viewport area on
// scroll/resize so a sticky drawer fills the available space without
// overflowing the viewport bottom. Shared by the AskTIM drawer and the feedback
// slot so both adjust to scroll position identically. `active` mirrors the
// AskTIM gate: visible AND not full-screen.
const useStickyDrawerHeight = (wrapperRef, active) => {
    useEffect(() => {
        const wrapper = wrapperRef.current;
        if (!wrapper) {
            return undefined;
        }
        if (!active) {
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

        mq.addEventListener('change', attach);
        attach();

        return () => {
            mq.removeEventListener('change', attach);
            detach();
        };
    }, [wrapperRef, active]);
};

const SidebarAIDrawerCoordinator = ({ courseId }) => {
    const contextValue = useContext(SidebarContext);
    const currentSidebar = contextValue?.currentSidebar ?? null;
    const toggleSidebar = contextValue?.toggleSidebar ?? (() => { });
    const shouldDisplayFullScreen = contextValue?.shouldDisplayFullScreen ?? false;
    const unitId = contextValue?.unitId ?? null;

    const [showAIDrawer, setShowAIDrawer] = useState(false);
    const [showFeedback, setShowFeedback] = useState(false);
    const prevUnitIdRef = useRef(unitId);
    const showAIDrawerRef = useRef(false);
    const showFeedbackRef = useRef(false);
    const wrapperRef = useRef(null);
    const feedbackWrapperRef = useRef(null);

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

        const messageType = event.data?.type;

        if (messageType && AI_DRAWER_MESSAGE_TYPES.includes(messageType)) {
            setShowAIDrawer(true);
            // Opening AskTIM hides feedback (they share this column).
            if (FEEDBACK_SLOT_MODE) {
                setShowFeedback(false);
                window.postMessage({ type: FEEDBACK_CLOSE_MESSAGE }, messageOrigin);
            }
            if (currentSidebar !== null) {
                toggleSidebar(null);
            }
        } else if (FEEDBACK_SLOT_MODE && messageType === FEEDBACK_OPEN_MESSAGE) {
            setShowFeedback(true);
            // Opening feedback hides AskTIM and the discussions sidebar.
            if (showAIDrawerRef.current) {
                window.postMessage({ type: AI_DRAWER_CLOSE_MESSAGE }, messageOrigin);
            }
            setShowAIDrawer(false);
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
            // Opening the discussions sidebar also hides feedback.
            if (FEEDBACK_SLOT_MODE) {
                setShowFeedback(false);
                window.postMessage({ type: FEEDBACK_CLOSE_MESSAGE }, messageOrigin);
            }
        }
    }, [currentSidebar, messageOrigin]);

    useEffect(() => {
        showAIDrawerRef.current = showAIDrawer;
    }, [showAIDrawer]);

    useEffect(() => {
        showFeedbackRef.current = showFeedback;
    }, [showFeedback]);

    useEffect(() => {
        if (prevUnitIdRef.current && prevUnitIdRef.current !== unitId && unitId !== null) {
            // Only send close message if drawer is actually open
            if (showAIDrawerRef.current) {
                window.postMessage(
                    {
                        type: AI_DRAWER_CLOSE_MESSAGE,
                    },
                    messageOrigin
                );
            }
            setShowAIDrawer(false);
            // Auto-close feedback on unit change too (mirrors AskTIM).
            if (FEEDBACK_SLOT_MODE) {
                if (showFeedbackRef.current) {
                    window.postMessage({ type: FEEDBACK_CLOSE_MESSAGE }, messageOrigin);
                }
                setShowFeedback(false);
            }
        }
        prevUnitIdRef.current = unitId;
    }, [unitId, messageOrigin]);

    // AskTIM and the feedback slot share the same sticky `.ai-drawer-wrapper`
    // sizing, so both track the available viewport space on scroll identically.
    useStickyDrawerHeight(wrapperRef, showAIDrawer && !shouldDisplayFullScreen);
    useStickyDrawerHeight(feedbackWrapperRef, showFeedback && !shouldDisplayFullScreen);

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
            {FEEDBACK_SLOT_MODE && (
                <div
                    ref={feedbackWrapperRef}
                    className={`ai-drawer-wrapper ml-0 ml-xl-4 align-top ${showFeedback ? '' : 'd-none'}`}
                    aria-hidden={!showFeedback}
                >
                    <FeedbackDrawerSlot />
                </div>
            )}
        </>
    );
};

SidebarAIDrawerCoordinator.propTypes = {
    courseId: PropTypes.string.isRequired,
};

export default SidebarAIDrawerCoordinator;
