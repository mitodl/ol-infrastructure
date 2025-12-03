import { useEffect, useRef, useState, useCallback } from 'react';
import { getConfig } from '@edx/frontend-platform';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';

const BUNDLE_PATH = process.env.AI_DRAWER_BUNDLE_PATH || '/learn/static/smoot-design/aiDrawerManager.umd.js';

let loadPromise = null;

const loadAiDrawerManagerBundle = () => {
    if (window.aiDrawerManager) {
        return Promise.resolve(window.aiDrawerManager);
    }

    if (loadPromise) {
        return loadPromise;
    }

    loadPromise = new Promise((resolve, reject) => {
        const existingScript = document.querySelector('script[data-ai-drawer-manager]');
        if (existingScript) {
            const handleLoad = () => {
                if (window.aiDrawerManager) {
                    resolve(window.aiDrawerManager);
                } else {
                    reject(new Error('Bundle loaded but aiDrawerManager not found on window'));
                }
            };

            const handleError = (error) => {
                reject(error || new Error('Failed to load existing script'));
            };

            existingScript.addEventListener('load', handleLoad, { once: true });
            existingScript.addEventListener('error', handleError, { once: true });
            return;
        }

        const script = document.createElement('script');
        script.src = BUNDLE_PATH;
        script.setAttribute('data-ai-drawer-manager', 'true');
        script.async = true;
        script.crossOrigin = 'anonymous';

        const handleLoad = () => {
            if (window.aiDrawerManager?.init) {
                resolve(window.aiDrawerManager);
            } else {
                reject(new Error('Bundle loaded but init function not found'));
            }
        };

        const handleError = () => {
            loadPromise = null;
            reject(new Error(`Failed to load aiDrawerManager bundle from ${BUNDLE_PATH}`));
        };

        script.addEventListener('load', handleLoad, { once: true });
        script.addEventListener('error', handleError, { once: true });

        document.head.appendChild(script);
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

const AiDrawerManagerSidebar = () => {
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

        loadAiDrawerManagerBundle()
            .then((aiDrawerManager) => {
                if (!isMounted || !containerRef.current) {
                    return;
                }

                try {
                    const initResult = aiDrawerManager.init(
                        {
                            messageOrigin: getMessageOrigin(),
                            variant: 'slot',
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
                        setInitError(getErrorMessage(error, 'Failed to initialize AiDrawerManager'));
                        setIsLoading(false);
                    }
                }
            })
            .catch((error) => {
                if (isMounted) {
                    setInitError(getErrorMessage(error, 'Failed to load aiDrawerManager bundle'));
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

AiDrawerManagerSidebar.propTypes = {};

export default AiDrawerManagerSidebar;
