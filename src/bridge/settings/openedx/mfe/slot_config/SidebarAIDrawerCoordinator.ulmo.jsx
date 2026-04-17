import React, { useContext, useEffect, useState, useCallback, useMemo, useRef } from 'react';
import PropTypes from 'prop-types';
import { getConfig } from '@edx/frontend-platform';
import { useModel } from '@src/generic/model-store';

import Sidebar from './src/courseware/course/sidebar/Sidebar';
import NewSidebar from './src/courseware/course/new-sidebar/Sidebar';
import SidebarContext from './src/courseware/course/sidebar/SidebarContext';
import NewSidebarContext from './src/courseware/course/new-sidebar/SidebarContext';
import AIDrawerManagerSidebar from './AIDrawerManagerSidebar';

const AI_DRAWER_MESSAGE_TYPES = [
    'smoot-design::ai-drawer-open',
    'smoot-design::tutor-drawer-open',
];

const SidebarAIDrawerCoordinator = ({ courseId }) => {
    const {
        isNewDiscussionSidebarViewEnabled,
    } = useModel('courseHomeMeta', courseId);

    const oldContextValue = useContext(SidebarContext);
    const newContextValue = useContext(NewSidebarContext);
    const contextValue = isNewDiscussionSidebarViewEnabled ? newContextValue : oldContextValue;
    const currentSidebar = contextValue?.currentSidebar ?? null;
    const toggleSidebar = contextValue?.toggleSidebar ?? (() => {});
    const shouldDisplayFullScreen = contextValue?.shouldDisplayFullScreen ?? false;
    const unitId = contextValue?.unitId ?? null;

    const [showAIDrawer, setShowAIDrawer] = useState(false);
    const prevUnitIdRef = useRef(unitId);
    const showAIDrawerRef = useRef(false);

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

    return (
        <>
            {currentSidebar !== null && (isNewDiscussionSidebarViewEnabled ? <NewSidebar /> : <Sidebar />)}
            <div
                className={`ai-drawer-wrapper ml-0 ml-xl-4 align-top ${
                    shouldDisplayFullScreen ? 'ai-drawer-wrapper-fullscreen' : ''
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
