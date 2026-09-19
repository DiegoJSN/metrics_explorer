class AuthorsRouter:
    def db_for_read(self, model, **hints):
        return 'authors' if model.__name__ in {'AuthorSummary','Work'} else None
    
    def db_for_write(self, model, **hints):
        return 'authors' if model.__name__ in {'AuthorSummary','Work'} else None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Never migrate these external tables
        if app_label == 'profiles':
            if model_name in {'authorsummary','work'}:
                return db == 'authors'
            # Job y el resto de modelos de profiles -> default
            return db == 'default'
        return None
