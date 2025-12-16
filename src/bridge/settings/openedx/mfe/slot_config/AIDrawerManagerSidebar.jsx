import { useEffect, useRef, useState, useCallback } from 'react';
import { getConfig } from '@edx/frontend-platform';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';

const BUNDLE_PATH = process.env.AI_DRAWER_BUNDLE_PATH || '/learn/static/smoot-design/aiDrawerManager.es.js';

let loadPromise = null;

const loadAIDrawerManagerBundle = () => {
    if (loadPromise) {
        return loadPromise;
    }

    loadPromise = import(
        // IMPORTANT: webpackIgnore prevents Webpack from bundling this dynamic import
        // This allows loading aiDrawerManager from a separate external bundle at runtime
        // DO NOT REMOVE - removing this will break the dynamic bundle loading mechanism
        /* webpackIgnore: true */
        BUNDLE_PATH
        )
        .then((module) => {
            if (!module?.init) {
                throw new Error('Bundle loaded but init function not found');
            }
            return module;
        })
        .catch((error) => {
            loadPromise = null;
            throw new Error(`Failed to load aiDrawerManager bundle from ${BUNDLE_PATH}: ${error.message}`);
        });

    return loadPromise;
};

const getMessageOrigin = () => {
    const lmsBaseUrl = getConfig().LMS_BASE_URL;
    if (lmsBaseUrl) {
        try {
            return new URL(lmsBaseUrl).origin;
        } catch (error) {
            // Fallback to window.location.origin
        }
    }
    return window.location.origin;
};

const getErrorMessage = (error, defaultMessage) => {
    return error instanceof Error ? error.message : defaultMessage;
};

const AIDrawerManagerSidebar = () => {
    const containerRef = useRef(null);
    const instanceRef = useRef(null);
    const [initError, setInitError] = useState(null);
    const [isLoading, setIsLoading] = useState(true);

    useEffect(() => {
        if (!containerRef.current) {
            return;
        }

        if (instanceRef.current) {
            setIsLoading(false);
            return;
        }

        let isMounted = true;

        loadAIDrawerManagerBundle()
            .then((aiDrawerManager) => {
                if (!isMounted || !containerRef.current) {
                    return;
                }

                try {
                    const initResult = aiDrawerManager.init(
                        {
                            messageOrigin: getMessageOrigin(),
                            variant: 'slot',
                            transformBody: messages => ({ message: messages[messages.length - 1].content }),
                            getTrackingClient: () => getAuthenticatedHttpClient(),
                        },
                        {
                            container: containerRef.current,
                        },
                    );

                    if (isMounted) {
                        instanceRef.current = initResult;
                        setInitError(null);
                        setIsLoading(false);
                    } else {
                        initResult.unmount();
                    }
                } catch (error) {
                    if (isMounted) {
                        setInitError(getErrorMessage(error, 'Failed to initialize AIDrawerManager'));
                        setIsLoading(false);
                    }
                }
            })
            .catch((error) => {
                if (isMounted) {
                    setInitError(getErrorMessage(error, 'Failed to load AIDrawerManager bundle'));
                    setIsLoading(false);
                }
            })
            .finally(() => {
                loadPromise = null;
            });

        return () => {
            isMounted = false;
            if (instanceRef.current?.unmount) {
                try {
                    instanceRef.current.unmount();
                } catch (error) {
                    // Unmount handles errors internally
                }
            }
            instanceRef.current = null;
        };
    }, []);

    return (
        <div
            ref={containerRef}
            className="ai-drawer-manager-sidebar-wrapper"
            role="region"
            aria-label="AI Chat Sidebar"
            aria-live="polite"
            aria-busy={isLoading}
        >
            {isLoading && (
                <div
                    className="d-flex align-items-center justify-content-center p-3"
                    role="status"
                    aria-label="Loading AI Chat"
                >
                    <div className="spinner-border spinner-border-sm" role="status">
                        <span className="sr-only">Loading AI Chat...</span>
                    </div>
                </div>
            )}
            {initError && (
                <div className="alert alert-danger" role="alert" aria-live="assertive">
                    <strong>Error loading AI Chat:</strong> {initError}
                </div>
            )}
        </div>
    );
};

AIDrawerManagerSidebar.propTypes = {};

export default AIDrawerManagerSidebar;
