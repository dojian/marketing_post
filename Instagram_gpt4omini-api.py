import autogen
import os
import webbrowser
from dotenv import load_dotenv
from autogen import ConversableAgent, UserProxyAgent, config_list_from_json, register_function
import agentops
import requests
import facebook
import urllib.parse
import re
from typing import Dict, Optional

from IPython.display import display
from PIL.Image import Image

from autogen.agentchat.contrib import img_utils
from autogen.agentchat.contrib.capabilities import generate_images
from autogen.cache import Cache
from autogen.oai import openai_utils

# Load environment variables from .env file
load_dotenv()

# Get the API key from the environment variable
app_id = os.getenv("FACEBOOK_ACCOUNT_ID")
app_secret = os.getenv("INSTAGRAM_APP_SECRET_KEY")
facebook_page_id = os.getenv("FACEBOOK_PAGE_ID")
redirect_uri = "http://localhost:8080"


# rapidapi_key = os.getenv("RAPIDAPI_KEY")

# AGENTOPS_API_KEY = os.getenv("agentops_api_key")
# agentops.init(AGENTOPS_API_KEY, default_tags=["autogen-tool-example"])

# print("AgentOps is now running. You can view your session in the link above")

# ## Setup

llm_config = {
    "seed": 42,
    "config_list": [{"model": "gpt-4o-mini", "api_key": os.environ["OPENAI_API_KEY"]}],
    "timeout": 120,
    "temperature": 0.7,
}
gpt_vision_config = {
    "config_list": [{"model": "gpt-4o", "api_key": os.environ["OPENAI_API_KEY"]}],
    "timeout": 120,
    "temperature": 0.7,
}
dalle_config = {
    "config_list": [{"model": "dall-e-2", "api_key": os.environ["OPENAI_API_KEY"]}],
    "timeout": 120,
    "temperature": 0.7,
}

# DEFINE TOOLS
def _is_termination_message(msg) -> bool:
    # Detects if we should terminate the conversation
    if isinstance(msg.get("content"), str):
        return msg["content"].rstrip().endswith("TERMINATE")
    elif isinstance(msg.get("content"), list):
        for content in msg["content"]:
            if isinstance(content, dict) and "text" in content:
                return content["text"].rstrip().endswith("TERMINATE")
    return False

def extract_images(sender: autogen.ConversableAgent, recipient: autogen.ConversableAgent) -> Image:
    images = []
    all_messages = sender.chat_messages[recipient]

    for message in reversed(all_messages):
        # The GPT-4V format, where the content is an array of data
        contents = message.get("content", [])
        for content in contents:
            if isinstance(content, str):
                continue
            if content.get("type", "") == "image_url":
                img_data = content["image_url"]["url"]
                images.append(img_utils.get_pil_image(img_data))

    if not images:
        raise ValueError("No image data found in messages.")

    return images

def post_image_to_facebook(caption: str, image_url: str) -> str:
    """Posts a message to your Facebook page.

    Args:
        caption (str): The caption to post with the image.
        image_url (str): The URL of the image to post.
    """
    # Create the authorization URL
    auth_url = (
        "https://www.facebook.com/v17.0/dialog/oauth?"
        + urllib.parse.urlencode(
            {
                "client_id": app_id,
                "redirect_uri": redirect_uri,
                "scope": "pages_read_engagement,pages_manage_posts",
                "response_type": "code"
            }
        )
    )
    print("Please visit this URL to authorize the app:", auth_url)
    webbrowser.open(auth_url)

    # After authorization, exchange the code for an access token
    code = input("Enter the authorization code from the redirect URL: ")

    # Exchange code for access token
    token_url = "https://graph.facebook.com/v17.0/oauth/access_token"
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "client_secret": app_secret,
        "code": code
    }
    response = requests.get(token_url, params=params)
    response_json = response.json()
    access_token = response_json.get("access_token")

    if access_token:
        graph = facebook.GraphAPI(access_token=access_token)
        print("Access token retrieved successfully!")
    else:
        print("Failed to get access token:", response_json)
        return 'Failed to get access token.'

    # Get image data from the URL
    image_data = requests.get(image_url).content

    # Post the image to Facebook
    response = graph.put_photo(
        image=image_data, 
        caption=caption, 
        page_id=facebook_page_id  # Replace with your Page ID
    )

    # Return the result based on the response
    if response:
        print('Image posted successfully!')
        return 'Image posted successfully!'
    else:
        print('Failed to post image.')
        return 'Failed to post image.'
    """Posts a message to your Facebook page.

    Args:
        caption (str): The caption to post with the image.
        image_url (str): The URL of the image to post.
    """
    # Get image data from the URL
    image_data = requests.get(image_url).content

    # Post the image to Facebook
    response = graph.put_photo(
        image=image_data, 
        caption=caption, 
        page_id=facebook_page_id  # Replace with your Page ID
    )

    # Return the result based on the response
    if response:
        print('Image posted successfully!')
        return 'Image posted successfully!'
    else:
        print('Failed to post image.')
        return 'Failed to post image.'

# ## The task!

task = input("Please enter the task you'd like to perform (e.g., 'Generate a marketing campaign post for Halloween'): ")

# ## Build a group chat
# 
# This group chat will include these agents:
# 
# 1. **User_proxy** or **Admin**: to allow the user to comment on the report and ask the writer to refine it.
# 2. **Planner**: to determine relevant information needed to complete the task.
# 3. **Engineer**: to write code using the defined plan by the planner.
# 4. **Executor**: to execute the code written by the engineer.
# 5. **Writer**: to write the report.

user_proxy = autogen.ConversableAgent(
    name="Admin",
    system_message="Give the task, and send "
    "instructions to planner.",
    code_execution_config=False,
    llm_config=llm_config,
    human_input_mode="ALWAYS",
)

planner = autogen.ConversableAgent(
    name="Planner",
    system_message="Given a task, please determine "
    "what information is needed to complete the task. "
    "Please note that the information will all be retrieved using"
    " Python code. Please only suggest information that can be "
    "retrieved using Python code. "
    "Dont suggest the actual code, pass the suggested parameters to the engineer and they will take care of the code."
    "After each step is done by others, check the progress and "
    "instruct the remaining steps. If a step fails, try to "
    "workaround",
    description="Planner. Given a task, determine what "
    "information is needed to complete the task. "
    "After each step is done by others, check the progress and "
    "instruct the remaining steps",
    llm_config=llm_config,
)

def image_generator_agent() -> autogen.ConversableAgent:
    # Create the agent
    agent = autogen.ConversableAgent(
        name="artist",
        llm_config=gpt_vision_config,
        max_consecutive_auto_reply=3,
        human_input_mode="NEVER",
        is_termination_msg=lambda msg: _is_termination_message(msg),
    )

    # Add image generation ability to the agent
    dalle_gen = generate_images.DalleImageGenerator(llm_config=dalle_config)
    image_gen_capability = generate_images.ImageGeneration(
        image_generator=dalle_gen, text_analyzer_llm_config=llm_config
    )

    image_gen_capability.add_to_agent(agent)
    return agent

artist = image_generator_agent()

engineer = autogen.AssistantAgent(
    name="Engineer",
    llm_config=llm_config,
    description="An engineer that writes code based on the plan "
    "provided by the planner. ",
    system_message="""You are a helpful AI assistant.
Solve tasks using your coding and language skills.
In the following cases, suggest python code (in a python coding block) or shell script (in a sh coding block) for the user to execute.
    1. When you need to collect info, use the code to output the info you need, for example, browse or search the web, download/read a file, print the content of a webpage or a file, get the current date/time, check the operating system. After sufficient info is printed and the task is ready to be solved based on your language skill, you can solve the task by yourself.
    2. When you need to perform some task with code, use the code to perform the task and output the result. Finish the task smartly.
Solve the task step by step if you need to. If a plan is not provided, explain your plan first. Be clear which step uses code, and which step uses your language skill.
When using code, you must indicate the script type in the code block. The user cannot provide any other feedback or perform any other action beyond executing the code you suggest. The user can't modify your code. So do not suggest incomplete code which requires users to modify. Don't use a code block if it's not intended to be executed by the user.
If you want the user to save the code in a file before executing it, put # filename: <filename> inside the code block as the first line. Don't include multiple code blocks in one response. Do not ask users to copy and paste the result. Instead, use 'print' function for the output when relevant. Check the execution result returned by the user.
If the result indicates there is an error, fix the error and output the code again. Suggest the full code instead of partial code or code changes. If the error can't be fixed or if the task is not solved even after the code is executed successfully, analyze the problem, revisit your assumption, collect additional info you need, and think of a different approach to try.
When you find an answer, verify the answer carefully. Include verifiable evidence in your response if possible.
You may need to call the function post_image_to_facebook(caption, image_url) to post to instagram. Use the `post_image_to_facebook` function to help the user to post the image and caption to their facebook account.

If the result indicates there is an error, fix the error and output the code again. Suggest the full code instead of partial code or code changes. If the error can't be fixed or if the task is not solved even after the code is executed successfully, analyze the problem, revisit your assumption, collect additional info you need, and think of a different approach to try.

Reply "TERMINATE" in the end when everything is done.
    """
)

# **Note**: In this lesson, you'll use an alternative method of code execution by providing a dict config. However, you can always use the LocalCommandLineCodeExecutor if you prefer. For more details about code_execution_config, check this: https://microsoft.github.io/autogen/docs/reference/agentchat/conversable_agent/#__init__

executor = autogen.ConversableAgent(
    name="Executor",
    system_message="Execute the code written by the "
    "engineer and report the result.",
    human_input_mode="NEVER",
    code_execution_config={
        "last_n_messages": 3,
        "work_dir": "coding",
        "use_docker": False,
    },
)

## Check Speaker Transition Policies
allowed_or_disallowed_speaker_transitions = {
    user_proxy: [planner],
    planner: [user_proxy, artist, engineer],
    artist: [user_proxy, planner, engineer],
    engineer: [executor, planner],
    executor: [engineer, planner],
}

## REGISTER THE TOOLS
register_function(
    post_image_to_facebook,
    caller=engineer,  # engineer assistant.
    executor=executor,  # execute engineer code by executor.
    name="post_image_to_facebook",  # By default, the function name is used as the tool name.
    description="A tool that post to facebook",  # A description of the tool.
)

# ## Define the group chat
groupchat = autogen.GroupChat(
    agents=[user_proxy, engineer, artist, executor, planner],
    messages=[],
    max_round=5,
)

manager = autogen.GroupChatManager(
    groupchat=groupchat, llm_config=llm_config
)

# ## Start the group chat!

groupchat_result = user_proxy.initiate_chat(
    manager,
    message=task,
)

# agentops.end_session("Success")