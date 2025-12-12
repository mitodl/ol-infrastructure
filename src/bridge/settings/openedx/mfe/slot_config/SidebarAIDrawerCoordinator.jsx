import React, { useContext, useEffect, useState, useCallback, useMemo } from 'react';
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

    const [showAiDrawer, setShowAiDrawer] = useState(false);

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

    const handleAiDrawerMessage = useCallback((event) => {
        if (event.origin !== messageOrigin) {
            return;
        }

        if (event.data?.type && AI_DRAWER_MESSAGE_TYPES.includes(event.data.type)) {
            setShowAiDrawer(true);
            if (currentSidebar !== null) {
                toggleSidebar(null);
            }
        }
    }, [messageOrigin, currentSidebar, toggleSidebar]);

    useEffect(() => {
        window.addEventListener('message', handleAiDrawerMessage);
        return () => {
            window.removeEventListener('message', handleAiDrawerMessage);
        };
    }, [handleAiDrawerMessage]);

    useEffect(() => {
        if (currentSidebar !== null) {
            setShowAiDrawer(false);
        }
    }, [currentSidebar]);

    return (
        <>
            {currentSidebar !== null && (isNewDiscussionSidebarViewEnabled ? <NewSidebar /> : <Sidebar />)}
            <div
                className={`ai-drawer-wrapper ml-0 ml-lg-4 h-auto align-top zindex-0 ${
                    shouldDisplayFullScreen ? 'ai-drawer-wrapper-fullscreen' : ''
                } ${showAiDrawer ? '' : 'd-none'}`}
                aria-hidden={!showAiDrawer}
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
