# requirements to add support for projects to deep-agents-sdk

Current the deep-agents-sdk implementation supports skills under skills/ folder. These skills refer to data residing under data/ . We need to add support for multiple projects, each with its own skills, data, and resources. Here are the requirements for multi-project support:

- Add a new variable to `.env` (and also the default template `.env.template`) called `PROJECTS_DIR`. This will point to the parent folder of all projects. The default value is `projects`. The `SKILLS_DIR` is no longer used. If skills exist for a project, they need to exist under a `skills` folder of that project. Under the skills subfolder, there could be multiple subfolders, each with its own skill. Every skill needs to be named `SKILL.md`.
- Each project will have a project name, whose name will be used to create a directory for it under the projects folder as defined in PROJECTS_DIR variable
- Each project will have multiple child folders. Currently we only use 2 child folders: data and skills. But others could be added and used per project.
- Use a database table to maintain a list of projects
- Create a new project called "HPI Analytics" under the projects/ folder. Move the skills/hpi-analysis/SKILL.md to this project's skills child folder. Also move the data directory, currently under the project's root, to under this new project
- The UI should be updated to look similar to the OpenAI Codex app, where the left pane shows a list of current projects.
- Add the ability to add a new project as well as the following:
  - Allow user to upload a zip file containing data, skills, etc.
  - The content of this zip file should be extracted under the new project's folder residing under PROJECTS_DIR
  - Allow the user to view the content of the zip before importing
  - When importing uploaded content, allow both merge mode and replace mode
- Allow users to view the contents of the selected project from the left pane
- Allow users to remove and upload new content of selected project
- When a user deletes a project, remove both its database record and its project folder from disk
- To issue prompts, users must first select a project. Upon selection, the system must load all skills metadata of the selected project into the skills registry, as currently done in the current implementation. In other word, each project has its own skills registry. This is critical dynamic loading of skills per prompt for the selected project
- For each project, users could have multiple chat sessions. Save each chat session so users can go back and view or continue where they left off
- The above functionality is very similar to how the current OpenAI Codex app works, except that is our implementation, we allow users to upload contents of each project. Also the skills registry for each project must be dynamically loaded upon selection
