package ch.claudesessiondeck.jetbrains

import com.intellij.openapi.components.service
import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity

/** Starts the [TabServer] once a project is open. */
class Startup : ProjectActivity {
    override suspend fun execute(project: Project) {
        service<TabServer>()
    }
}
