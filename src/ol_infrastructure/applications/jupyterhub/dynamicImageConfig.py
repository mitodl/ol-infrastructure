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
            if course in KNOWN_COURSES:
                self.image = image_base.format(course)
        return super().start()


c.JupyterHub.spawner_class = QueryStringKubeSpawner  # noqa: F821
c.Authenticator.allow_all = True  # noqa: F821
