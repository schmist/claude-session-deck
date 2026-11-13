package ch.claudesessiondeck.jetbrains;

import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.startup.ProjectActivity;
import kotlin.Unit;
import kotlin.coroutines.Continuation;

/** Starts the {@link TabServer} once a project is open. */
public final class Startup implements ProjectActivity {
    @Override
    public Object execute(Project project, Continuation<? super Unit> continuation) {
        ApplicationManager.getApplication().getService(TabServer.class);
        return Unit.INSTANCE;
    }
}
