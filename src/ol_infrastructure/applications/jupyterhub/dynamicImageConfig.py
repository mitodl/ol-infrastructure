from kubespawner import KubeSpawner


class QueryStringKubeSpawner(KubeSpawner):
    def start(self):
        image_base = (
            "610119931565.dkr.ecr.us-east-1.amazonaws.com/ol-course-notebooks:{}"
        )
        KNOWN_COURSES = [
            "clustering_and_descriptive_ai",
            "deep_learning_foundations_and_applications",
            "supervised_learning_fundamentals",
            "introduction_to_data_analytics_and_machine_learning",
        ]
        self.image = (
            "610119931565.dkr.ecr.us-east-1.amazonaws.com/"
            "ol-course-notebooks:clustering_and_descriptive_ai"
        )
        if self.handler:
            course = self.handler.get_query_argument("course", "").lower()
            # Notebook is case sensitive, and special characters
            # with query string meaning must be URL encoded i.e.
            # notebook=Assignment_2_Prediction_with_LogReg_%26_CART_%26_XGBoost.ipynb
            notebook = self.handler.get_query_argument("notebook", "")
            if course in KNOWN_COURSES:
                self.image = image_base.format(course)
                # If we don't have a notebook, don't muck with default_url
                # This falls back to the tree view in Jupyterhub if not specified
                if notebook:
                    self.default_url = f"/notebooks/{notebook}"
        return super().start()


c.JupyterHub.spawner_class = QueryStringKubeSpawner  # type: ignore[name-defined] # noqa: F821
c.Authenticator.allow_all = True  # type: ignore[name-defined] # noqa: F821
